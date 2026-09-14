"""Push-парсер SSE: собрать chat.completion из чанков, отдавая дельты живьём.

План async-переезда, Задача 6. Раньше сборка была pull-циклом «for payload in
итератор» — из асинхронного кода такой цикл не позвать, а источник строк там
асинхронный (``httpx``: ``async for line in r.aiter_lines()``). Второй копии
сборки не хочется: она знает про склейку tool_calls по индексам, про error
внутри 200-стрима и про ``<think>``, разорванный между чанками, — разъехавшись,
эти три места сломались бы по-разному и молча.

Поэтому состояние вынесено в объект: кто читает строки — не его забота.
Синхронный транспорт кормит его из ``r.iter_lines()``, асинхронный из
``r.aiter_lines()``; логика одна.

Результат байт-совместим с ``r.json()`` не-стримового ответа: downstream не
должен знать, каким путём пришёл ответ.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

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


def _first_choice(chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    ch0 = choices[0]
    return ch0 if isinstance(ch0, dict) else None


class _InlineThinkSplitter:
    """Route streamed `content` into think/answer segments, tracking
    `<think>...</think>` across chunk boundaries. Tags split between chunks are
    buffered until resolved. Tag characters themselves are consumed (not emitted)."""

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def _kind(self) -> str:
        return "think" if self._in_think else "answer"

    def _switch_at(self, idx: int, tag: str, emit: OnDelta) -> None:
        before = self._buf[:idx]
        if before:
            emit(self._kind(), before)
        self._buf = self._buf[idx + len(tag):]
        self._in_think = not self._in_think

    def _flush_keeping_partial(self, tag: str, emit: OnDelta) -> None:
        keep = _partial_prefix_len(self._buf, tag)
        cut = len(self._buf) - keep
        flush_now = self._buf[:cut]
        if flush_now:
            emit(self._kind(), flush_now)
        self._buf = self._buf[cut:]

    def feed(self, text: str, emit: OnDelta) -> None:
        self._buf += text
        while self._buf:
            tag = _THINK_CLOSE if self._in_think else _THINK_OPEN
            idx = self._buf.find(tag)
            if idx == -1:
                self._flush_keeping_partial(tag, emit)
                return
            self._switch_at(idx, tag, emit)

    def flush(self, emit: OnDelta) -> None:
        if self._buf:
            emit("think" if self._in_think else "answer", self._buf)
            self._buf = ""


class StreamAssembler:
    """Состояние сборки одного стрима. Кормить построчно или попейлоадно.

        asm = StreamAssembler(on_delta=sink)
        for line in r.iter_lines():        # или async for ... in r.aiter_lines()
            asm.feed_line(line)
        asm.flush()
        response = asm.result()

    ``feed_line`` сам разбирает SSE-обёртку (`data: …`, `[DONE]`); после
    ``[DONE]`` дальнейшие строки игнорируются — стрим кончился, а источник
    может дослать хвост.
    """

    def __init__(self, on_delta: Optional[OnDelta] = None) -> None:
        self._on_delta = on_delta
        self._splitter = _InlineThinkSplitter() if on_delta is not None else None
        self._content_parts: List[str] = []
        self._reasoning_parts: List[str] = []
        self._tool_calls: Dict[int, Dict[str, Any]] = {}
        self._role = "assistant"
        self._finish_reason: Optional[str] = None
        self._usage: Optional[Dict[str, Any]] = None
        self._model: Optional[str] = None
        self._resp_id: Optional[str] = None
        self.done = False

    # --- вход ---------------------------------------------------------------

    def feed_line(self, line: Any) -> None:
        """Скормить одну строку SSE (`str` или `bytes`)."""
        if self.done:
            return
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8", errors="replace")
        line = (line or "").strip()
        if not line or not line.startswith("data:"):
            return
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            self.done = True
            return
        self.feed_payload(data)

    def feed_payload(self, payload: str) -> None:
        """Скормить JSON-пейлоад одного чанка (без обёртки `data:`)."""
        try:
            chunk = json.loads(payload)
        except (ValueError, TypeError):
            return
        if not isinstance(chunk, dict):
            return
        # Провайдер вернул error-событие внутри 200-стрима — не глотать: поднять
        # с реальным сообщением, иначе сборка выйдет пустой и downstream даст
        # загадочное «Unknown finish reason».
        self._absorb_chunk_meta(chunk)
        ch0 = _first_choice(chunk)
        if ch0 is None:
            return
        choice_err = ch0.get("error")
        if choice_err is not None:
            _raise_stream_error(choice_err)
        if ch0.get("finish_reason"):
            self._finish_reason = ch0["finish_reason"]
        delta = ch0.get("delta") or {}
        if not isinstance(delta, dict):
            return
        self._absorb_delta(delta)

    def _absorb_chunk_meta(self, chunk: Dict[str, Any]) -> None:
        stream_err = chunk.get("error")
        if stream_err is not None:
            _raise_stream_error(stream_err)
        if chunk.get("usage") is not None:
            self._usage = chunk["usage"]
        if self._model is None and chunk.get("model"):
            self._model = chunk["model"]
        if self._resp_id is None and chunk.get("id"):
            self._resp_id = chunk["id"]

    def _absorb_delta(self, delta: Dict[str, Any]) -> None:
        if delta.get("role"):
            self._role = delta["role"]

        rc = delta.get("reasoning_content")
        if not (isinstance(rc, str) and rc):
            rc = delta.get("reasoning")
        if isinstance(rc, str) and rc:
            self._reasoning_parts.append(rc)
            if self._on_delta is not None:
                self._on_delta("think", rc)

        c = delta.get("content")
        if isinstance(c, str) and c:
            self._content_parts.append(c)
            if self._splitter is not None:
                self._splitter.feed(c, self._on_delta)

        for tc in (delta.get("tool_calls") or []):
            if isinstance(tc, dict):
                self._absorb_tool_call(tc)

    def _absorb_tool_call(self, tc: Dict[str, Any]) -> None:
        idx = tc.get("index", 0)
        slot = self._tool_calls.setdefault(
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
            if self._on_delta is not None:
                self._on_delta("tool", fn["arguments"])

    # --- выход --------------------------------------------------------------

    def flush(self) -> None:
        """Дослать остаток буфера ``<think>``-сплиттера. Идемпотентно."""
        if self._splitter is not None:
            self._splitter.flush(self._on_delta)

    def result(self) -> Dict[str, Any]:
        """Собранный ответ в форме не-стримового ``r.json()``.

        ``finish_reason`` может остаться ``None``, если стрим оборвался, — это
        намеренно: downstream увидит ошибку вместо подделанного значения.
        """
        message: Dict[str, Any] = {
            "role": self._role,
            "content": "".join(self._content_parts),
        }
        if self._reasoning_parts:
            message["reasoning_content"] = "".join(self._reasoning_parts)
        if self._tool_calls:
            message["tool_calls"] = [self._tool_calls[i] for i in sorted(self._tool_calls)]

        result: Dict[str, Any] = {
            "id": self._resp_id,
            "model": self._model,
            "choices": [
                {"index": 0, "message": message, "finish_reason": self._finish_reason}
            ],
        }
        if self._usage is not None:
            result["usage"] = self._usage
        return result


__all__ = [
    "BackendStreamError",
    "OnDelta",
    "StreamAssembler",
]
