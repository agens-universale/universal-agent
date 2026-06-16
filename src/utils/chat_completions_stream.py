"""SSE parsing + reassembly for streamed chat/completions.

Pure transport helpers: reassemble an OpenAI-compatible chat.completion object
from streamed SSE chunks, byte-compatible with the non-stream `r.json()` shape,
while emitting (kind, text) deltas for live display. No business logic.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

OnDelta = Callable[[str, str], None]

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

    Emits on_delta("think"|"answer", text) per chunk: reasoning_content -> think,
    content -> answer. Inline <think> handling is left to the display layer (the
    content is preserved verbatim here). `finish_reason` may be `None` if the
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
        if ch0.get("finish_reason"):
            finish_reason = ch0["finish_reason"]
        delta = ch0.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        if delta.get("role"):
            role = delta["role"]

        rc = delta.get("reasoning_content")
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
