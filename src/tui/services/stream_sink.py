"""StreamSink — thread-safe buffer for live chat/completions deltas (TUI-only).

Fed by RunContext.on_stream_delta from the agent worker thread; drained by the
TUI poll tick. `format_stream_block` renders a compact in-progress view: the
answer text plus a single tail line of reasoning (full <think> is never dumped).
Presentation layer — no business logic, not a substitute for the thread store.
"""
from __future__ import annotations

import re
import threading

_THINK_RE = re.compile(r"<think>(.*?)(?:</think>|$)", re.DOTALL)


class StreamSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._think = ""
        self._answer = ""

    def push(self, kind: str, text: str) -> None:
        with self._lock:
            if kind == "reset":
                self._think = ""
                self._answer = ""
            elif kind == "think":
                self._think += text
            else:
                self._answer += text

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            return self._think, self._answer

    def has_content(self) -> bool:
        with self._lock:
            return bool(self._think or self._answer)

    def reset(self) -> None:
        with self._lock:
            self._think = ""
            self._answer = ""


def _tail_line(s: str) -> str:
    lines = [ln for ln in s.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def format_stream_block(*, think: str, answer: str) -> str:
    """Compact in-progress view. Inline <think> spans in `answer` are stripped
    out (their tail shown as reasoning), leaving only the real answer text."""
    inline_think = " ".join(m.group(1) for m in _THINK_RE.finditer(answer))
    answer_clean = _THINK_RE.sub("", answer).strip()
    think_tail = _tail_line(inline_think) or _tail_line(think)

    lines = []
    if think_tail:
        lines.append(f"⠿ думаю… {think_tail}")
    if answer_clean:
        lines.append(answer_clean)
    return "\n".join(lines)
