"""Бренд-контент TUI GIGACODE — единый источник имени и презентационных строк.

Презентационный слой: константы + чистые функции рендера (без бизнес-логики).
Все экраны импортируют строки отсюда, чтобы имя/строки не дублировались.
"""
from __future__ import annotations

from rich.text import Text

from gc_tui.palette import ACCENT, OLIVE, RUN_COLOR

APP_NAME = "GigaCode"

# Логотип ANSI Shadow «GIGACODE» (псевдографика CP437: █ ╗ ╝ ═ ║).
# Осознанное исключение из правила «только ASCII» — разовый брендовый момент.
LOGO = (
    " ██████╗ ██╗ ██████╗  █████╗  ██████╗ ██████╗ ██████╗ ███████╗\n"
    "██╔════╝ ██║██╔════╝ ██╔══██╗██╔════╝██╔═══██╗██╔══██╗██╔════╝\n"
    "██║  ███╗██║██║  ███╗███████║██║     ██║   ██║██║  ██║█████╗  \n"
    "██║   ██║██║██║   ██║██╔══██║██║     ██║   ██║██║  ██║██╔══╝  \n"
    "╚██████╔╝██║╚██████╔╝██║  ██║╚██████╗╚██████╔╝██████╔╝███████╗\n"
    " ╚═════╝ ╚═╝ ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝"
)

# Позиционирующее заявление под логотипом.
TAGLINE = "Universal Agent"

# Приветственная строка для свежего экрана (хедер-слева, нет активной задачи).
WELCOME = "Добро пожаловать в GigaCode"

# Стопы градиента логотипа: амбер → олива → teal (всё из палитры).
_WARM = RUN_COLOR
_OLIVE = OLIVE
_TEAL = ACCENT


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_to_hex(c: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*c)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def gradient_color(frac: float) -> str:
    """Цвет колонки на нормированной позиции frac∈[0,1]: WARM→OLIVE→TEAL."""
    frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
    warm, olive, teal = _hex_to_rgb(_WARM), _hex_to_rgb(_OLIVE), _hex_to_rgb(_TEAL)
    if frac <= 0.5:
        return _rgb_to_hex(_lerp(warm, olive, frac / 0.5))
    return _rgb_to_hex(_lerp(olive, teal, (frac - 0.5) / 0.5))


def logo_text() -> Text:
    """Логотип как rich.Text с поколоночным градиентом (пробелы не красятся)."""
    lines = LOGO.split("\n")
    width = max((len(line) for line in lines), default=1)
    t = Text()
    for li, line in enumerate(lines):
        if li:
            t.append("\n")
        for col, ch in enumerate(line):
            if ch == " ":
                t.append(" ")
            else:
                frac = col / (width - 1) if width > 1 else 0.0
                t.append(ch, style=gradient_color(frac))
    return t


def tagline_display() -> str:
    """`Universal Agent` → `U N I V E R S A L   A G E N T` (капс + разрядка)."""
    return "   ".join(" ".join(word) for word in TAGLINE.upper().split())


# Разрядка тэглайна (как `.logo-sub` в gc-ua-install.html: airy letter-spacing,
# заметный gap между словами). Межсловный зазор — двойной от межбуквенного.
# Оба меняются одним числом.
_TAGLINE_LETTER_GAP = "  "   # 2 пробела — между буквами
_TAGLINE_WORD_GAP = "    "    # 4 пробела — между словами (двойной от letter-gap)


def version_under_logo() -> str:
    """Версия отдельной строкой под тэглайном, центрированная по ширине лого — тем же
    приёмом, что и сам тэглайн (ведущие пробелы, а не CSS: рамка карточки шире бренда,
    и align центрировал бы по ней). Воздух над строкой даёт CSS (`.brand-version`).
    Из исходников показывается «dev», см. app_version.display_version."""
    from gc_core import app_version
    text = app_version.display_version(long=True)   # в карточке есть место на пояснение
    logo_w = max((len(line) for line in LOGO.splitlines()), default=0)
    return " " * max(0, (logo_w - len(text)) // 2) + text


def tagline_under_logo() -> str:
    """Тэглайн под логотипом: разрядка букв (`_TAGLINE_LETTER_GAP`), межсловный
    зазор (`_TAGLINE_WORD_GAP`, двойной), центрирование по ширине лого (ведущие
    пробелы). Стиль — как `.logo-sub` в docs/gc-ua-install.html. Сам
    отступ-строка над тэглайном — CSS (`.brand-tagline { margin-top: 1 }`)."""
    tag = _TAGLINE_WORD_GAP.join(
        _TAGLINE_LETTER_GAP.join(word) for word in TAGLINE.upper().split()
    )
    logo_w = max((len(line) for line in LOGO.split("\n")), default=0)
    pad = max(0, (logo_w - len(tag)) // 2)
    return " " * pad + tag
