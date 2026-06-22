"""Цвет бейджа статуса сессии (чистая презентация)."""
from __future__ import annotations

from tui.palette import ACCENT, DIM, ERR, OK, RUN_COLOR

_COLORS = {
    "[running]": RUN_COLOR,
    "[asking]": ACCENT,
    "[completed]": OK,
    "[stopped]": DIM,
    "[failed]": ERR,
}


def status_badge_color(badge: str) -> str:
    """Цвет палитры для бейджа (`"[running]"` …); неизвестный → DIM."""
    return _COLORS.get(badge, DIM)
