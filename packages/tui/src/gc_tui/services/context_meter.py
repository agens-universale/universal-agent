"""Индикатор заполненности контекста сессии для шапки TUI (чистая презентация).

Плашка фикс-ширины `_WIDTH` ячеек: реверс-бренд-градиент (ACCENT/teal → RUN_COLOR/amber)
как ФОН заполненной части, тусклый трек — под пустой; белый текст «контекст NN%»
поверх. Реверс = «нагрев»: amber-край проявляется по мере заполнения. Фон на
пробелах-ячейках — терминал-независимо (без shade-глифов).
Спека: docs/tui/specs/2026-07-05-context-gradient-meter-design.md.
"""
from __future__ import annotations

from rich.style import Style
from rich.text import Text

from gc_tui.branding import gradient_color
from gc_tui.palette import TEXT_ON_FILL, TRACK

# Ширина плашки в ячейках — фиксированный размер под футер.
_WIDTH = 21
# «Трек» пустой части: приглушённый серый, видимый над тёмным фоном футера, но
# подчинённый цветной заливке.
_TRACK = TRACK
_LABEL = "контекст"
_WHITE = TEXT_ON_FILL


def _cell_bg(i: int, filled: int) -> str:
    """Фон ячейки i: реверс-градиент по АБСОЛЮТНОЙ позиции если залито, иначе трек.

    Реверс: i=0 → gradient_color(1)=ACCENT(teal); i=_WIDTH-1 → gradient_color(0)=RUN_COLOR(amber).
    Хью привязан к колонке, поэтому amber-край проявляется только когда заливка дошла.
    """
    if i >= filled:
        return _TRACK
    frac = i / (_WIDTH - 1) if _WIDTH > 1 else 0.0
    return gradient_color(1.0 - frac)


def render_context_meter(chars: int, threshold: int) -> Text | None:
    """Rich-Text плашки «контекст … NN%» шириной _WIDTH ячеек, или None если скрыто.

    Заполнено = реверс-градиент-фон (нагрев к amber), пусто = тусклый трек; текст белый
    поверх, при pct>90 проценты — bold (критический сигнал близкого форс-сжатия).
    """
    if threshold <= 0:
        return None
    pct = min(100, round(100 * chars / threshold))
    filled = round(_WIDTH * pct / 100)

    # Раскладка контента: «контекст» с col 1 (ячейка воздуха слева), «NN%» — справа
    # с симметричной ячейкой воздуха (последняя колонка пустая, как col 0 слева).
    pct_str = f"{pct}%"
    cells = [" "] * _WIDTH
    for k, ch in enumerate(_LABEL):
        pos = 1 + k
        if pos < _WIDTH:
            cells[pos] = ch
    pct_start = _WIDTH - len(pct_str) - 1
    for k, ch in enumerate(pct_str):
        cells[pct_start + k] = ch

    t = Text()
    for i, ch in enumerate(cells):
        bold = ch != " " and i >= pct_start and pct > 90   # проценты жирным при кризисе
        t.append(ch, style=Style(color=_WHITE, bgcolor=_cell_bg(i, filled), bold=bold))
    return t
