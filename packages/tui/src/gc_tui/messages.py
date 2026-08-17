"""Сообщения между виджетами трейса и экраном."""
from __future__ import annotations

from textual.message import Message


class OpenEventRequest(Message):
    """Запрос открыть полный payload события (двойной клик или Enter по строке)."""

    def __init__(self, event_no: int) -> None:
        self.event_no = event_no
        super().__init__()


class OpenToolCallDetailRequest(Message):
    """Двойной клик / Enter на ToolCallRow → детальный экран с call + output."""
    def __init__(self, call_id: str) -> None:
        super().__init__()
        self.call_id = call_id


class SummaryToggleRequest(Message):
    """Клик/Enter по маркеру или телу сводки → TraceScreen раскрывает/сворачивает блок."""
    def __init__(self, event_no: int) -> None:
        super().__init__()
        self.event_no = event_no
