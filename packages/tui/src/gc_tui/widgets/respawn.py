"""Копия эфемерного виджета ленты для памяти эфемерного (спека шага 2
окна ленты, §5.4): снятый ход вернётся с ней.

Свои виджеты ленты умеют копию сами (`respawn()` — новый, ещё не
смонтированный виджет в нынешнем виде); простая строка `Static` (ошибка
команды) копируется здесь. Остальное не копируется — None.
"""

from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static


def respawn(widget: Widget) -> Widget | None:
    own = getattr(widget, "respawn", None)
    if callable(own):
        return own()
    if type(widget) is Static:
        return Static(
            widget.content, classes=" ".join(widget.classes), markup=False
        )
    return None
