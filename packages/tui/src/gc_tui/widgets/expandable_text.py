"""Тело раскрытого текста строки ленты: весь текст и `− свернуть` внизу.

Общее для строки мысли модели (`Thought`) и вызова `Think`: строка
раскрывает тело кликом по своему тексту (или пробелом), фраза
`− свернуть` (класс ховера `.trace-action`) постит `ExpandableToggle` —
сворачивает владелец. Цвет текста — от владельца (CSS)."""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from gc_tui.widgets.no_select import NoSelectStatic

EXPANDABLE_COLLAPSE = "− свернуть"


class ExpandableToggle(Message):
    """Клик по `− свернуть` тела: владелец сворачивает текст."""


class _CollapsePhrase(NoSelectStatic):
    """`− свернуть`: служебный элемент — клик по нему строку не выделяет
    и фокус не берёт (`click_model.focus_target_at`)."""

    FOCUS_ON_CLICK = False

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(ExpandableToggle())


class ExpandableTextBody(Widget):
    """Весь текст строки (перенос по словам) и `− свернуть` последней
    строкой; текст дорастает над фразой (`set_text`)."""

    DEFAULT_CSS = """
    ExpandableTextBody {
        height: auto;
        width: 1fr;
    }
    ExpandableTextBody > .expandable-text { width: 1fr; height: auto; }
    ExpandableTextBody > .expandable-collapse { width: auto; height: 1; }
    """

    def __init__(self, text: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._text = Static(text, classes="expandable-text", markup=False)

    def compose(self) -> ComposeResult:
        yield self._text
        yield _CollapsePhrase(
            EXPANDABLE_COLLAPSE,
            classes="expandable-collapse trace-action",
            markup=False,
        )

    def set_text(self, text: str) -> None:
        self._text.update(text)
