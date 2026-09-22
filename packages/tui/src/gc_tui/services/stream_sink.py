"""StreamSink — thread-safe buffer for live chat/completions deltas (TUI-only).

Fed by RunContext.on_stream_delta from the agent worker thread; drained by the
TUI poll tick. `format_stream_block` renders the answer text only (inline <think>
stripped); reasoning is surfaced via the ThinkingIndicator. The sink also tracks
a self-counted progress counter and reasoning start/end timing for that indicator.
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
        self._reasoning_ended_at = None  # float | None

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
            elif kind == "tool":
                # Аргументы вызова инструмента. В _think/_answer им НЕЛЬЗЯ: на них
                # смотрит has_content(), и блок потока показал бы сырой JSON. Но в
                # счётчик они идут — это те же пришедшие из потока токены, а на шаге
                # с вызовом инструмента других не бывает вовсе.
                self._chars += len(text)
                # Рассуждение закончилось: модель перешла к выводу. Без этого шаг
                # «рассуждение → вызов» навсегда оставался бы в состоянии "thinking",
                # потому что фазу закрывала только ветка ответа.
                if self._reasoning_started_at is not None and self._reasoning_ended_at is None:
                    self._reasoning_ended_at = self._clock()
            else:  # "answer" (default)
                self._answer += text
                self._chars += len(text)
                if self._reasoning_started_at is not None and self._reasoning_ended_at is None:
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
    """In-progress answer text for the StreamBlock (answer phase). Reasoning is
    surfaced separately — `format_reasoning_line` (the StreamBlock's thinking-phase
    line) and the ThinkingIndicator counter — not here. Inline <think>...</think>
    spans (incl. an unclosed one mid-stream) are stripped."""
    return _THINK_RE.sub("", answer).strip()


def _reasoning_tail(think: str, maxlen: int) -> str:
    """Reasoning as a single line: collapse whitespace; if longer than `maxlen`,
    keep the last `maxlen` chars trimmed to a word boundary (no mid-word cut),
    '…'-prefixed."""
    one_line = " ".join(think.split())
    if not one_line:
        return ""
    if len(one_line) <= maxlen:
        return one_line
    tail = one_line[-maxlen:]
    sp = tail.find(" ")
    if sp != -1:  # drop the leading partial word → start at a word boundary
        tail = tail[sp + 1 :]
    return "…" + tail


def format_reasoning_line(*, think: str, tail_len: int = 64) -> str:
    """Compact one-line reasoning glimpse for the StreamBlock during the
    thinking phase: `⠇ <tail>` (empty string if no reasoning)."""
    tail = _reasoning_tail(think, tail_len)
    return f"⠇ {tail}" if tail else ""


def select_stream_render(mode: str, clean_answer: str, reasoning_line: str) -> tuple[str, bool]:
    """Что показать в StreamBlock по режиму tui_stream_display.
    Возвращает (kind, thinking): kind ∈ {"answer", "reasoning", "none"}."""
    if mode == "minimal":
        return ("none", False)
    if clean_answer.strip():
        return ("answer", False)
    if mode == "answer_reasoning" and reasoning_line:
        return ("reasoning", True)
    return ("none", False)
