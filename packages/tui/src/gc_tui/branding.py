"""Бренд-контент TUI GIGACODE — единый источник имени и презентационных строк.

Презентационный слой: константы + чистые функции рендера (без бизнес-логики).
Все экраны импортируют строки отсюда, чтобы имя/строки не дублировались.
"""

from __future__ import annotations

from rich.console import Console, ConsoleOptions, RenderResult
from rich.measure import Measurement
from rich.text import Text

from gc_tui.palette import (
    ACCENT,
    DIM,
    GRADIENT_COOL,
    GRADIENT_MID,
    GRADIENT_WARM,
    INK,
    MUTED,
)
from gc_tui.services.column_row import ColumnRow, WidthAware

APP_NAME = "GigaCode"

# Позиционирующее заявление под логотипом.
TAGLINE = "Universal Agent"

# Приветственная строка для свежего экрана (хедер-слева, нет активной задачи).
WELCOME = "Добро пожаловать в GigaCode"

# Стопы градиента логотипа: амбер → олива → teal (свои токены палитры).
_WARM = GRADIENT_WARM
_OLIVE = GRADIENT_MID
_TEAL = GRADIENT_COOL


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_to_hex(c: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*c)


def _lerp(
    a: tuple[int, int, int], b: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def gradient_color(frac: float) -> str:
    """Цвет колонки на нормированной позиции frac∈[0,1]: WARM→OLIVE→TEAL."""
    frac = min(1.0, max(0.0, frac))
    warm, olive, teal = (
        _hex_to_rgb(_WARM),
        _hex_to_rgb(_OLIVE),
        _hex_to_rgb(_TEAL),
    )
    if frac <= 0.5:
        return _rgb_to_hex(_lerp(warm, olive, frac / 0.5))
    return _rgb_to_hex(_lerp(olive, teal, (frac - 0.5) / 0.5))


# Вордмарк стартовой шапки: две строки по 29 колонок.
MARK = (
    "█▀▀ █ █▀▀ ▄▀█ █▀▀ █▀█ █▀▄ █▀▀",
    "█▄█ █ █▄█ █▀█ █▄▄ █▄█ █▄▀ ██▄",
)
UPDATE_TAIL = ("↑ доступна новая версия", " · /settings → Обновление")

_MARK_W = len(MARK[0])
_GAP = "   "
_TAG_W = len(TAGLINE)


def version_label() -> str:
    from gc_core import app_version

    return app_version.display_version(long=True)


def _gradient_text(line: str, *, span: int) -> Text:
    """Строка с градиентом по колонкам на общую ширину ``span``."""
    text = Text(no_wrap=True)
    for col, ch in enumerate(line):
        if ch == " ":
            text.append(ch)
        else:
            frac = col / (span - 1) if span > 1 else 0.0
            text.append(ch, style=f"bold {gradient_color(frac)}")
    return text


class BrandHeader(WidthAware):
    """Блок бренда: знак из двух строк, подпись и версия. Раскладка — по
    ширине, которую дал рендер (`options.max_width`): узко — один ряд."""

    def __init__(
        self,
        version: str,
        *,
        update: bool = False,
        status: Text | ColumnRow | None = None,
    ) -> None:
        self.version = version
        self.update = update
        self.status = status

    @property
    def _mark_need(self) -> int:
        return _MARK_W + len(_GAP) + max(_TAG_W, len(self.version))

    @staticmethod
    def _tail_atoms() -> list[Text]:
        """Хвост обновления: стрелка с текстом акцентом, адрес настроек — DIM."""
        return [
            Text(UPDATE_TAIL[0], style=ACCENT),
            Text(UPDATE_TAIL[1], style=DIM),
        ]

    def _tail_beside(self) -> int:
        """Ширина строки версии вместе с хвостом (две клетки зазора)."""
        tail = len(UPDATE_TAIL[0]) + len(UPDATE_TAIL[1])
        wide = _MARK_W + len(_GAP) + len(self.version)
        return wide + 2 + tail

    def _mark_rows(self, beside: bool) -> list[Text]:
        top = _gradient_text(MARK[0], span=_MARK_W)
        top.append(_GAP + TAGLINE, style=INK)
        bottom = _gradient_text(MARK[1], span=_MARK_W)
        bottom.append(_GAP + self.version, style=MUTED)
        if beside:
            bottom.append("  ")
            for atom in self._tail_atoms():
                bottom.append_text(atom)
        return [top, bottom]

    def _single_row(self) -> ColumnRow:
        head = _gradient_text(f"{APP_NAME}  ", span=len(APP_NAME))
        atoms = [
            Text(f"{TAGLINE}  ", style=INK),
            Text(self.version, style=MUTED),
        ]
        return ColumnRow(head, atoms)

    def _status(self) -> RenderResult:
        # Строка загрузки сама не переносится (`loading_line`): копия не нужна.
        yield Text("")
        yield self.status

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = options.max_width
        wide = width >= self._mark_need
        beside = wide and self.update and width >= self._tail_beside()
        if wide:
            yield from self._mark_rows(beside)
        else:
            yield self._single_row()
        if self.status is not None:
            yield from self._status()
        if self.update and not beside:
            yield ColumnRow(Text("  "), self._tail_atoms())

    def __rich_measure__(
        self, console: Console, options: ConsoleOptions
    ) -> Measurement:
        widest = self._tail_beside() if self.update else self._mark_need
        return Measurement(widest, widest)
