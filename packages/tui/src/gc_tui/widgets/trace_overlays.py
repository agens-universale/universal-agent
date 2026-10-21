"""Накладки над лентой, когда она не внизу (спека P3 §2.6).

`StickyReply` — первая строка ленты: ближайшая реплика выше верхнего края.
`ScrollEndPill` — по центру последней строки ленты: «в конец».
Обе — `overlay: screen` с абсолютной позицией по `#trace.content_region`:
высоты ленты не занимают, фокус и выделение не берут."""

from __future__ import annotations

from typing import Optional

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events
from textual.geometry import Region
from textual.widget import Widget
from textual.widgets import Static

from gc_tui.messages import ScrollToEnd, ScrollToReply
from gc_tui.palette import DIM, MUTED
from gc_tui.services.trace_render import USER_PREFIX

#: Подпись плашки: курсор не в поле или в поле есть текст.
PILL_CLICK = "↓ в конец · клик"
#: Подпись плашки: курсор в пустом поле — Enter тоже ведёт к низу.
PILL_ENTER = "↓ в конец · клик или Enter"


class _Overlay(Widget):
    """Общее накладок: не в фокусе, не в выделении, клик — сообщение,
    колесо — прокрутка ленты `scroll_target` под накладкой."""

    ALLOW_SELECT = False
    FOCUS_ON_CLICK = False

    def __init__(self, scroll_target: Widget, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._scroll_target = scroll_target

    def _scroll_by(self, event: events.MouseEvent, step: int) -> None:
        # Как колесо над самой лентой: шаг — чувствительность приложения.
        # Сразу, без отсрочки: иначе `scroll_target_y` до обновления
        # прежний, и пачка тиков колеса сливается в один шаг.
        event.stop()
        target = self._scroll_target
        target.scroll_to(
            y=target.scroll_target_y + step * self.app.scroll_sensitivity_y,
            animate=False,
            immediate=True,
        )

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._scroll_by(event, -1)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._scroll_by(event, 1)

    def _place(self, x: int, y: int, width: int) -> None:
        self.styles.offset = (x, y)
        self.styles.width = width
        self.display = True

    def get_selection(self, selection):
        return None

    def _click_message(self):
        raise NotImplementedError

    def on_click(self, event: events.Click) -> None:
        # Клик не доходит до экрана: тот увёл бы фокус в ленту.
        event.stop()
        self.post_message(self._click_message())


class StickyReply(_Overlay):
    """`› текст…` ближайшей реплики выше края; клик — к реплике."""

    def __init__(self, scroll_target: Widget, **kwargs) -> None:
        super().__init__(scroll_target, **kwargs)
        self.reply: Optional[Widget] = None

    def show(self, reply: Optional[Widget], box: Region) -> None:
        """Показать реплику `reply` над первой строкой `box`; `None` —
        скрыть."""
        if reply is None:
            self.reply = None
            self.display = False
            return
        if reply is not self.reply:
            self.reply = reply
            self.refresh()
        self._place(box.x, box.y, box.width)

    def render(self) -> Text:
        text = " ".join(getattr(self.reply, "plain_text", "").split())
        line = Text(USER_PREFIX, style=Style(color=MUTED), no_wrap=True)
        line.append(text, style=Style(color=DIM))
        line.truncate(self.size.width, overflow="ellipsis")
        return line

    def _click_message(self) -> ScrollToReply:
        return ScrollToReply(self.reply)


class ScrollEndPill(_Overlay, Static):
    """`↓ в конец · …` по центру последней строки; клик — к низу."""

    def __init__(self, scroll_target: Widget, **kwargs) -> None:
        super().__init__(scroll_target, "", markup=False, **kwargs)
        self.label = ""

    def show(self, label: Optional[str], box: Region) -> None:
        """Показать подпись `label` по центру последней строки `box`;
        `None` — скрыть."""
        if label is None:
            self.display = False
            return
        if label != self.label:
            self.label = label
            self.update(f" {label} ")
        width = cell_len(label) + 2
        x = box.x + (box.width - width) // 2
        self._place(x, box.bottom - 1, width)

    def _click_message(self) -> ScrollToEnd:
        return ScrollToEnd()
