"""Индикатор заполненности контекста сессии для шапки TUI (чистая презентация)."""
from __future__ import annotations

from rich.text import Text

from gc_tui.palette import OK, RUN_COLOR, WARN

_CELLS = 8
# Один надёжный глиф (full block U+2588) на всю шкалу — заполненную/пустую часть
# различаем ЦВЕТОМ, а не плотностью shade-символов (░▓). Shade-глифы зависят от
# шрифта терминала: на части Windows-консолей «пустой» ░ рендерится чёрным/пропадает
# — была видна только заполненная часть. Full block рендерится везде одинаково.
_BLOCK = "█"
# «Трек» пустой части: приглушённый серый, видимый над тёмным фоном футера, но
# подчинённый цветной заливке.
_TRACK = "#3f4654"


def render_context_meter(chars: int, threshold: int) -> Text | None:
    """Rich-Text «контекст ████████ 58%» или None, если показывать нечего.

    Заполненная часть окрашена по порогам (OK/RUN/WARN), пустая — тусклый трек;
    оба сегмента — один глиф █, различие только в цвете (терминал-независимо).
    """
    if threshold <= 0:
        return None
    pct = min(100, round(100 * chars / threshold))
    filled = round(_CELLS * pct / 100)
    if pct < 60:
        color = OK
    elif pct <= 90:
        color = RUN_COLOR
    else:
        color = WARN
    t = Text()
    t.append("контекст ", style=color)
    t.append(_BLOCK * filled, style=color)
    t.append(_BLOCK * (_CELLS - filled), style=_TRACK)
    t.append(f" {pct}%", style=color)
    return t
