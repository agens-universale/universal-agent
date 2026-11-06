"""Полоса позиции `/view` для файла, который показывается окнами.

Встроенный скроллбар области текста знает только текущее окно и врёт о
файле: то его нет, то ползунок во всю высоту. Эта полоса считается по всему
файлу — высота ползунка — доля видимых строк, положение — где окно в файле.
Вид — общего скроллбара (`styles.tcss`, `VerticalScroll`): серый ползунок,
бирюзовый под мышью. Клик — прыжок в это место файла, колесо — листание."""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from rich.text import Text
from textual import events
from textual.widget import Widget

from gc_tui import palette

# Строк за один шаг колеса — как у прокрутки ленты.
WHEEL_LINES = 3


def thumb_span(
    start: int, page: int, total: Optional[int], height: int
) -> tuple[int, int]:
    """(верх, высота) ползунка в строках полосы высотой ``height``; файл
    целиком в окне или длина неизвестна — ползунка нет: ``(0, 0)``."""
    if height <= 0 or not total or total <= page:
        return 0, 0
    size = max(1, round(height * page / total))
    top = round((start - 1) * height / total)
    return min(top, height - size), size


class FilePositionBar(Widget):
    DEFAULT_CSS = "FilePositionBar { width: 1; height: 1fr; }"

    def __init__(
        self,
        *,
        on_jump: Callable[[float], None],
        on_wheel: Callable[[int], None],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._on_jump = on_jump
        self._on_wheel = on_wheel
        self._start = 1
        self._page = 1
        self._total: Optional[int] = None
        self._hover = False

    def set_position(
        self, start: int, page: int, total: Optional[int]
    ) -> None:
        self._start, self._page, self._total = start, page, total
        self.refresh()

    def render(self) -> Text:
        height = self.size.height
        top, size = thumb_span(self._start, self._page, self._total, height)
        thumb = palette.ACCENT if self._hover else palette.DEEP_DIM
        rows = [
            Text(
                " ",
                style=f"on {thumb if top <= i < top + size else palette.BG}",
            )
            for i in range(height)
        ]
        return Text("\n").join(rows)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_jump(event.y / max(1, self.size.height))

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self._on_wheel(WHEEL_LINES)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self._on_wheel(-WHEEL_LINES)

    def on_enter(self, event: events.Enter) -> None:
        self._hover = True
        self.refresh()

    def on_leave(self, event: events.Leave) -> None:
        self._hover = False
        self.refresh()
