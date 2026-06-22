"""Индикатор заполненности контекста сессии для шапки TUI (чистая презентация)."""
from __future__ import annotations

from tui.palette import OK, RUN_COLOR, WARN

_CELLS = 8


def render_context_meter(chars: int, threshold: int) -> tuple[str, str] | None:
    """`(text, color)` для индикатора или None, если показывать нечего.

    text = "контекст ▓▓▓▓▓░░░ 58%"; color — токен палитры по порогам.
    """
    if threshold <= 0:
        return None
    pct = min(100, round(100 * chars / threshold))
    filled = round(_CELLS * pct / 100)
    bar = "▓" * filled + "░" * (_CELLS - filled)
    if pct < 60:
        color = OK
    elif pct <= 90:
        color = RUN_COLOR
    else:
        color = WARN
    return f"контекст {bar} {pct}%", color
