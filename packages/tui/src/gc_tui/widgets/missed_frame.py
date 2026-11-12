"""Протокол «пропустил кадр»: перерисовка виджета за экраном пропущена.

Перерисовка невидимого пересобирает карту экрана, поэтому живые виджеты
ленты (мигание, счётчики, стартовая шапка) за экраном не рисуют, а
отмечают пропуск `miss_frame`. Экран с лентой (`TraceFollowMixin`) ведёт
таких слабым множеством и после раскладки зовёт у них
`redraw_if_missed()`.

Контракт: кто зовёт `miss_frame(self)`, обязан иметь поле
`_frame_missed` и метод `redraw_if_missed() -> bool` — помечен и виден:
перерисоваться, снять отметку и вернуть `True`; иначе `False`."""

from __future__ import annotations

from textual.dom import NoScreen
from textual.widget import Widget


def miss_frame(widget: Widget) -> None:
    """Перерисовка `widget` пропущена за экраном: отметка
    `_frame_missed` и запись у экрана с лентой — он освежит виджет, когда
    тот станет виден (`redraw_if_missed`). Экран без ленты или виджет без
    экрана — только отметка."""
    widget._frame_missed = True
    try:
        note = getattr(widget.screen, "note_missed_frame", None)
    except NoScreen:
        return
    if note is not None:
        note(widget)
