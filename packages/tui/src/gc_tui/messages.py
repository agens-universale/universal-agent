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


class CopyBlockRequest(Message):
    """Кнопка ❐ или двойной клик по блоку → экран копирует payload целиком.

    Виджет не обращается к буферу сам: главный экран дополнительно пишет копию
    в буфер сессии, экран превью — нет (там нет своей активной сессии)."""

    def __init__(self, payload: str) -> None:
        super().__init__()
        self.payload = payload


class OpenImageRequest(Message):
    """Ctrl+клик по плашке или подстроке картинки → экран открывает файл
    просмотрщиком ОС (или показывает отказ, если файла нет)."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
