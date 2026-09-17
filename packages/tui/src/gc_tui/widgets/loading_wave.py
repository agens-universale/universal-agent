"""Бегущая точка-волна загрузки (визуальный контракт §2): три точки `·`,
подсвеченная (teal) перебегает по WAVE_SEQ. Общий helper — хаб /settings и
MCP-секция используют один и тот же индикатор."""
from __future__ import annotations

from rich.text import Text

from gc_tui.palette import ACCENT, DEEP_DIM

WAVE_SEQ = (0, 1, 2, 1)
WAVE_INTERVAL = 0.16


def wave_glyph(frame: int) -> Text:
    lit = WAVE_SEQ[frame % len(WAVE_SEQ)]
    t = Text()
    for j in range(3):
        t.append("·", style=ACCENT if j == lit else DEEP_DIM)
    return t
