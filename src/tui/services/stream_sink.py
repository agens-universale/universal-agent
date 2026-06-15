"""StreamSink — thread-safe buffer for live chat/completions deltas (TUI-only).

Fed by RunContext.on_stream_delta from the agent worker thread; drained by the
TUI poll tick. `format_stream_block` renders a compact in-progress view: the
answer text plus a single tail line of reasoning (full <think> is never dumped).
Presentation layer — no business logic, not a substitute for the thread store.
"""
from __future__ import annotations

import re
import threading
import time

_THINK_RE = re.compile(r"<think>(.*?)(?:</think>|$)", re.DOTALL)


class StreamSink:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._think = ""
        self._answer = ""
        self._chars = 0
        self._reasoning_started_at = None  # float | None
        self._reasoning_ended_at = None    # float | None

    def push(self, kind: str, text: str) -> None:
        with self._lock:
            if kind == "reset":
                self._think = ""
                self._answer = ""
                self._chars = 0
                self._reasoning_started_at = None
                self._reasoning_ended_at = None
                return
            if kind == "think":
                self._think += text
                self._chars += len(text)
                if self._reasoning_started_at is None:
                    self._reasoning_started_at = self._clock()
            else:  # "answer" (default)
                self._answer += text
                self._chars += len(text)
                if (
                    self._reasoning_started_at is not None
                    and self._reasoning_ended_at is None
                ):
                    self._reasoning_ended_at = self._clock()

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            return self._think, self._answer

    def has_content(self) -> bool:
        with self._lock:
            return bool(self._think or self._answer)

    def progress_units(self) -> int:
        """~tokens received from the stream (chars // 4). Liveness counter —
        self-measured, independent of backend `usage`."""
        with self._lock:
            return self._chars // 4

    def reasoning_state(self):
        """(None, None) | ("thinking", None) | ("done", duration_sec)."""
        with self._lock:
            if self._reasoning_started_at is None:
                return (None, None)
            if self._reasoning_ended_at is None:
                return ("thinking", None)
            return ("done", self._reasoning_ended_at - self._reasoning_started_at)

    def reset(self) -> None:
        with self._lock:
            self._think = ""
            self._answer = ""
            self._chars = 0
            self._reasoning_started_at = None
            self._reasoning_ended_at = None


def format_stream_block(*, answer: str) -> str:
    """In-progress view for the StreamBlock: ONLY the answer text.
    Reasoning is surfaced via the ThinkingIndicator, not here. Inline
    <think>...</think> spans (incl. an unclosed one mid-stream) are stripped."""
    return _THINK_RE.sub("", answer).strip()
