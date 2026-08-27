"""SSE parsing + reassembly for streamed chat/completions.

Pure transport helpers: reassemble an OpenAI-compatible chat.completion object
from streamed SSE chunks, byte-compatible with the non-stream `r.json()` shape,
while emitting (kind, text) deltas for live display. No business logic.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

OnDelta = Callable[[str, str], None]


class BackendStreamError(ValueError):
    """SSE-стрим принёс error-событие: провайдер вернул ошибку ВНУТРИ 200-стрима
    (`data: {"error": …}`), а не HTTP-статусом. Раньше такой чанк молча
    пропускался → пустая сборка → downstream «Unknown finish reason» (маскировка).
    Несёт реальное сообщение провайдера. Подкласс ValueError — существующие
    `except ValueError` продолжают ловить, но тип различим для ретрай-политики.

    ``status_code`` — код из error-события (напр. 400), чтобы адаптивная политика
    ретраев трактовала стрим-ошибку так же, как HTTP-ошибку того же кода
    (паритет stream ↔ non-stream: в non-stream это был бы HTTP 4xx/5xx)."""

    def __init__(self, message: str, *, status_code: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _raise_stream_error(err: Any) -> None:
    code = err.get("code") if isinstance(err, dict) else None
    raise BackendStreamError(f"Backend stream error: {err}", status_code=code)


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _partial_prefix_len(buf: str, tag: str) -> int:
    """Longest suffix of `buf` that is a proper prefix of `tag` (a tag possibly
    split across chunk boundaries — hold it back until the next chunk)."""
    for k in range(min(len(buf), len(tag) - 1), 0, -1):
        if tag.startswith(buf[-k:]):
            return k
    return 0


class _InlineThinkSplitter:
    """Route streamed `content` into think/answer segments, tracking
    `<think>...</think>` across chunk boundaries. Tags split between chunks are
    buffered until resolved. Tag characters themselves are consumed (not emitted)."""

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def feed(self, text: str, emit: OnDelta) -> None:
        self._buf += text
        while self._buf:
            tag = _THINK_CLOSE if self._in_think else _THINK_OPEN
            idx = self._buf.find(tag)
            if idx != -1:
                before = self._buf[:idx]
                if before:
                    emit("think" if self._in_think else "answer", before)
                self._buf = self._buf[idx + len(tag):]
                self._in_think = not self._in_think
                continue
            keep = _partial_prefix_len(self._buf, tag)
            cut = len(self._buf) - keep
            flush_now = self._buf[:cut]
            if flush_now:
                emit("think" if self._in_think else "answer", flush_now)
            self._buf = self._buf[cut:]
            break

    def flush(self, emit: OnDelta) -> None:
        if self._buf:
            emit("think" if self._in_think else "answer", self._buf)
            self._buf = ""


def iter_sse_data_lines(raw_lines: Iterable) -> Iterable[str]:
    """Yield JSON payload strings from SSE `data:` lines; stop at `[DONE]`."""
    for line in raw_lines:
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8", errors="replace")
        line = (line or "").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        yield data


def assemble_streamed_completion(
    data_payloads: Iterable[str],
    on_delta: Optional[OnDelta] = None,
) -> Dict[str, Any]:
    """Reassemble a chat.completion object from SSE chunk JSON payloads.

    Emits on_delta("think"|"answer"|"tool", text): reasoning and content inside
    inline `<think>...</think>` -> think; content outside -> answer.
    `message.content` is preserved verbatim (raw `<think>` included; downstream
    strips it). `finish_reason` may be `None` if the
    stream ends without one (e.g. a truncated/interrupted stream); this is
    intentional and surfaces downstream as an error rather than being masked by a
    fake default.
    """
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: Dict[int, Dict[str, Any]] = {}
    role = "assistant"
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    resp_id: Optional[str] = None
    splitter = _InlineThinkSplitter() if on_delta is not None else None

    for payload in data_payloads:
        try:
            chunk = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if not isinstance(chunk, dict):
            continue
        # Провайдер вернул error-событие внутри 200-стрима — не глотать: поднять
        # с реальным сообщением, иначе сборка выйдет пустой и downstream даст
        # загадочное «Unknown finish reason».
        stream_err = chunk.get("error")
        if stream_err is not None:
            _raise_stream_error(stream_err)
        if chunk.get("usage") is not None:
            usage = chunk["usage"]
        if model is None and chunk.get("model"):
            model = chunk["model"]
        if resp_id is None and chunk.get("id"):
            resp_id = chunk["id"]

        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        ch0 = choices[0]
        if not isinstance(ch0, dict):
            continue
        choice_err = ch0.get("error")
        if choice_err is not None:
            _raise_stream_error(choice_err)
        if ch0.get("finish_reason"):
            finish_reason = ch0["finish_reason"]
        delta = ch0.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        if delta.get("role"):
            role = delta["role"]

        rc = delta.get("reasoning_content")
        if not (isinstance(rc, str) and rc):
            # Второе имя того же поля: OpenRouter шлёт рассуждение как `reasoning`
            # (проверено живой пробой 27.08.2026 на anthropic/claude-opus-5). Берём
            # ОДНО из двух, а не сумму: провайдер, приславший оба, удвоил бы и текст
            # рассуждения, и счётчик прогресса.
            rc = delta.get("reasoning")
        if isinstance(rc, str) and rc:
            reasoning_parts.append(rc)
            if on_delta is not None:
                on_delta("think", rc)

        c = delta.get("content")
        if isinstance(c, str) and c:
            content_parts.append(c)
            if splitter is not None:
                splitter.feed(c, on_delta)

        for tc in (delta.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)  # OpenAI-compatible providers always send index; fallback 0 collapses all tool calls into one slot for non-compliant providers (accepted for OpenAI-compatible transport)
            slot = tool_calls.setdefault(
                idx,
                {"id": None, "type": "function",
                 "function": {"name": "", "arguments": ""}},
            )
            if tc.get("id"):
                slot["id"] = tc["id"]
            if tc.get("type"):
                slot["type"] = tc["type"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]
                if on_delta is not None:
                    # Отдельный вид, а не "answer": аргументы — не текст ответа, в
                    # блок потока им нельзя (иначе на экран полезет сырой JSON).
                    # Но это ЕДИНСТВЕННОЕ, что модель генерирует на шаге с вызовом
                    # инструмента, и без этой дельты счётчик прогресса на таком шаге
                    # оставался нулём. У агента же почти каждый шаг такой — включая
                    # финальный ответ, который едет в аргументе turn_finish.
                    on_delta("tool", fn["arguments"])

    if splitter is not None:
        splitter.flush(on_delta)

    message: Dict[str, Any] = {"role": role, "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    result: Dict[str, Any] = {
        "id": resp_id,
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ],
    }
    if usage is not None:
        result["usage"] = usage
    return result
