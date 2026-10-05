"""Канон ``content``: трейс внутри ``<think>``, затем финальная проза."""

from __future__ import annotations

import re
from typing import Any

from gc_core.message_projection.projectors.strip_tagged_blocks import (
    strip_think_blocks,
)

_OPEN = "<think>\n"
_CLOSE = "</think>\n"
_IN_PROGRESS = "in_progress"
_CLOSE_TAG = re.compile(r"</think>", re.IGNORECASE)


def break_think_close(text: str) -> str:
    """Литерал ``</think>`` в трейсе не закрывает блок фасада."""
    return _CLOSE_TAG.sub(lambda match: "< " + match.group(0)[1:], text)


class ContentMap:
    """Склейка дельт хода в одну строку сообщения ассистента."""

    def __init__(self) -> None:
        self._think_open = False
        self._at_bol = True
        self._answer = ""
        self._titles: dict[str, str] = {}
        self.parts: list[str] = []

    @property
    def content(self) -> str:
        return "".join(self.parts)

    def feed(self, kind: str, text: str) -> list[str]:
        if kind == "reset":
            return self._reset()
        if kind == "think":
            return self._trace(text, bol_before=False)
        if kind == "answer" and isinstance(text, str):
            self._answer += text
        return []

    def apply_tool(self, report: dict) -> list[str]:
        op = report.get("op")
        if op == "open":
            title = str(report.get("title") or "")
            call_id = str(report.get("call_id") or "")
            self._titles[call_id] = title
            return self._trace(f"{title} — {_IN_PROGRESS}\n", bol_before=True)
        if op == "close":
            call_id = str(report.get("call_id") or "")
            title = self._titles.get(call_id, call_id)
            status = str(report.get("status") or "")
            return self._trace(f"{title} — {status}\n", bol_before=True)
        return []

    def finish(self, final: Any) -> list[str]:
        prose = strip_think_blocks(final if isinstance(final, str) else "")
        chunks: list[str] = []
        if self._think_open:
            if not self._at_bol:
                chunks.append("\n")
            chunks.append(_CLOSE)
            self._think_open = False
            self._at_bol = True
        if prose:
            chunks.append(prose)
        self.parts.extend(chunks)
        self._answer = ""
        return chunks

    def _reset(self) -> list[str]:
        buf = self._answer
        self._answer = ""
        if buf == "":
            return []
        return self._trace(buf, bol_before=True)

    def _trace(self, text: str, *, bol_before: bool) -> list[str]:
        if not isinstance(text, str) or text == "":
            return []
        chunks: list[str] = []
        if not self._think_open:
            chunks.append(_OPEN)
            self._think_open = True
            self._at_bol = True
        if bol_before and not self._at_bol:
            chunks.append("\n")
            self._at_bol = True
        safe = break_think_close(text)
        chunks.append(safe)
        self._at_bol = safe.endswith("\n")
        self.parts.extend(chunks)
        return chunks
