"""Цветовые токены TUI — единый источник правды для CSS и Python-рендера.

Здесь определяются ВСЕ цвета. `styles.tcss` их не дублирует: `TuiApp.get_css_variables()`
инжектит `css_variables()` в набор CSS-переменных, поэтому `$code`, `$ok`, … в TCSS
резолвятся из этого модуля. Код, красящий через `rich.Text(..., style=...)`, берёт те
же константы напрямую. Одно значение — один источник.

Имена констант соответствуют TCSS-переменным (`$deep-dim` → `DEEP_DIM`,
`$run-color` → `RUN_COLOR`, ...).
"""

from __future__ import annotations

from collections.abc import Callable

# Базовые
BG = "#0E1119"
INK = "#ebe9e6"
BRIGHT = "#eef2f6"
DIM = "#8b949e"
# Второстепенные ключи (дети пресета: /gate, /answers) — середина между INK и
# DIM по каждому каналу: тише основных ключей, но явно светлее подсказок.
SUB_INK = "#bbbec2"
MUTED = "#6e7681"
DEEP_DIM = "#7a8290"

# Семантические
ACCENT = "#54B3A6"
CODE = "#afbdff"  # инлайн-код / путь / тех. имя (тулы, сервера, ключи)
TOOL_NAME = "#BCC3CC"
CODE_BLOCK = "#9aa4b2"
OK = "#5FAE7F"
RUN_COLOR = "#D4A574"
OLIVE = "#8FA05A"  # средний стоп градиента логотипа (амбер→олива→teal)
ERR = "#C75C54"
WARN = "#f0883e"  # предупреждение (не ошибка): контекст-метр, ⚠-уведомления
INFO = "#58a6ff"  # ℹ-уведомления (info-уровень)
BRAND = "#9A6DD7"

# Плашки футера (контекст-метр, голосовая пилюля)
TRACK = "#3f4654"  # «трек» незаполненной части метра: серый над тёмным футером
TEXT_ON_FILL = "#ffffff"  # текст поверх цветной заливки плашек (метр, пилюля)

# Поверхности
BAND_BG = "#1f2128"
# Блочный контент ленты (код, таблицы, диффы) — тише полосы баннеров: блок
# должен читаться карточкой, но не «выпирать» из почти чёрного трейса.
# Дистанция до BG: 15.6 против 27.7 у BAND_BG (метрика _distance тестов палитры).
BLOCK_BG = "#171a22"
FOCUS_BG = "#363b47"  # подсветка строки трассы в фокусе (только CSS :focus) — светлее user-band

# Выделение текста мышью. Тот же синий, которым Textual выделяет текст в
# TextArea (экран /buffer): #368AE9 с прозрачностью 40% поверх фона даёт
# примерно это значение — интерфейс говорит про выделение одним цветом.
#
# Почему не светлее: комментарии в подсветке кода тёмные, и на более светлом
# фоне их контраст падает ниже читаемого. Пара «этот фон + комментарии на $dim»
# (см. services/code_render) подобрана так, чтобы читаемым осталось всё.
# Границы закреплены тестами в packages/tui/tests/test_palette_sync.py.
SCREEN_SELECTION_BACKGROUND = "#1e416c"
CARD_SELECT_BG = "#2e323d"  # выбранная карточка (/sessions, /skills, /mcp) — мягче focus-bg, светлее band-bg
FRAME_ARCHIVE = "#4b525d"  # серая рамка «архив/история» (отвеченный вопрос)
NOTE_INK = "#a5adb8"  # ремарка: чуть светлее DIM
USER_BAND_BG = "#282c36"  # подложка реплики: светлее BAND_BG, темнее FOCUS_BG
OVERLAY_BG = "#161A23"  # фон оверлеев (палитра, сессии, предпросмотр)
ZEBRA_BG = "#232836"  # чётная строка зебры (справка)
SKILL = "#afd7ff"  # имя скилла


def _rgb(hex_color: str) -> tuple[int, ...]:
    value = hex_color.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def _hex(channels) -> str:
    clamped = (min(255, max(0, round(c))) for c in channels)
    return "#" + "".join(f"{c:02x}" for c in clamped)


def _mix(base: str, accent: str, t: float) -> str:
    """Смешивает accent с base в пропорции t∈[0,1] → hex (base при t=0, accent при t=1)."""
    mixed = (bc + (ac - bc) * t for bc, ac in zip(_rgb(base), _rgb(accent)))
    return _hex(mixed)


# Диффы правок агента: фоны строк — примесь OK/ERR к BG (не «чистые» red/green,
# дифф остаётся в гамме интерфейса), intraline-акценты — более насыщенная примесь
# того же цвета, рисуется поверх фона строки. Коэффициент 0.29 — минимум, при
# котором ADD_BG и DEL_BG расходятся на дистанцию >=40 (порог неразличимости
# двух тёмных поверхностей в этом проекте, см. test_selection_distinguishable_from_focus):
# при 0.29 дистанция ≈41.3, при 0.4 — уже ≈56.1. Старое значение 0.15 (коэффициент
# ФОНОВ в другой паре, selection/focus, до их разведения) дало бы здесь только
# ≈21.5 — под порогом. Коэффициент HL поднят вслед за фоном (0.5, дистанция ≈70.4),
# чтобы акцент остался различим над новым, более насыщенным фоном строки.
# В теме auto фоны диффа сдвигаются вместе с фоном терминала (_DIFF, _derived).
DIFF_ADD_BG = _mix(BG, OK, 0.29)
DIFF_DEL_BG = _mix(BG, ERR, 0.29)
DIFF_ADD_HL = _mix(BG, OK, 0.5)
DIFF_DEL_HL = _mix(BG, ERR, 0.5)
DIFF_GUTTER = MUTED  # колонка номеров строк — существующий приглушённый токен
# Номер строки и маркер +/- красятся по ТИПУ строки ОДНИМ цветом: структура
# правки считывается по колонке слева, не читая код.
#
# Это РОВНО семантические OK/ERR — те же зелёный галочек «✓» и красный ошибок,
# что уже приняты в интерфейсе. Дифф не заводит собственный оттенок «почти
# зелёного»: один смысл — один цвет во всём TUI.
#
# Две попытки-предшественницы (обе отвергнуты на живом смоуке 2026-08-10-11):
# примесь к приглушённому MUTED (0.55) вышла ТЕМНЕЕ и самого OK, и самого ERR —
# читалась выцветшей; примесь к светлому INK (0.7) подняла светимость до
# 0.455 / 0.323, но обесцветила — на глаз всё равно «бледно». Насыщенность здесь
# важнее светимости.
#
# Границы (метрика _distance / _luminance тестов палитры):
# * номер и маркер лежат НА заливке строки (она идёт от начала строки, включая
#   колонку) — дистанция до DIFF_ADD_BG / DIFF_DEL_BG 144.5 / 147.4 при пороге
#   различимости 40; у КОНТЕКСТНОЙ строки заливки нет, и там номер по-прежнему
#   на фоне блока — дистанция 189.0 / 194.5 против 158.3 у DIFF_GUTTER;
# * пара add/del расходится на 139.2 (порог различимости 40);
# * колонка не спорит с кодом: светимость 0.342 / 0.204 против 0.366 у CODE_BLOCK.
DIFF_ADD_GUTTER = OK
DIFF_DEL_GUTTER = ERR


# Кромка неотправленной реплики; в теме auto — _mix(BG, ACCENT, 0.64).
USER_PENDING_EDGE = "#3a7a72"


# Рампы анимаций (ping-pong, пик — токен палитры); значения те же, что были
# литералами в виджетах. В теме auto их пересчитывает apply_background.
# Дыхание «думаю»: gold, пик #f0c896.
GLOW_SHADES = [
    "#9c7a4a",
    "#b8915c",
    RUN_COLOR,
    "#e0b585",
    "#ecc596",
    "#f0c896",
    "#ecc596",
    "#e0b585",
    RUN_COLOR,
    "#b8915c",
]
# Точка «сверяюсь…»: тёмно-серая, пик DEEP_DIM.
PROCESSING_SHADES = [
    "#4c525b",
    "#545b65",
    "#5d646e",
    "#666e78",
    "#6f7782",
    "#7a8290",
    "#6f7782",
    "#666e78",
    "#5d646e",
    "#545b65",
]
# Искра «завершаю…»: серая, пик CODE_BLOCK.
VALIDATING_SHADES = [
    "#5e6675",
    "#69727f",
    "#747e8b",
    "#808c99",
    "#8c98a6",
    "#9aa4b2",
    "#8c98a6",
    "#808c99",
    "#747e8b",
    "#69727f",
]
# Пульс running-глифа ▸: амбер, пик RUN_COLOR.
PULSE_SHADES = [
    "#8a6a3a",
    "#977448",
    "#a47f55",
    "#b18a62",
    "#c4986b",
    RUN_COLOR,
    "#c4986b",
    "#b18a62",
    "#a47f55",
    "#977448",
]


# ── Тема auto: палитра от фона терминала ───────────────────────────────────
# Всё выше — тема gigacode (литералы, снапшоты неизменны). apply_background
# подменяет фон и зависящие от него токены; reset возвращает литералы.

# Тон синеватых поверхностей (фокус, карточки, оверлеи, трек метра). Один
# параметр: "#ffffff" даёт нейтральные поверхности той же светлоты. Подобран
# так, что при фоне gigacode каждая поверхность расходится с литералом <= 2.
SURFACE_TINT = "#78829b"
_TINTED = (
    "FOCUS_BG",
    "CARD_SELECT_BG",
    "USER_BAND_BG",
    "OVERLAY_BG",
    "ZEBRA_BG",
    "TRACK",
)
# Фоны диффа едут вместе с фоном на тот же сдвиг: их взаимные дистанции
# (стражи различимости add/del, акцент/строка, строка/фон) не зависят от
# фона. Примесь _mix(bg, OK, 0.29) на светлом фоне теряла их ниже порога.
_DIFF = ("DIFF_ADD_BG", "DIFF_DEL_BG", "DIFF_ADD_HL", "DIFF_DEL_HL")
# Рампа → её пик (сам пик от фона не зависит).
_RAMP_PEAKS = {
    "GLOW_SHADES": "#f0c896",
    "PROCESSING_SHADES": DEEP_DIM,
    "VALIDATING_SHADES": CODE_BLOCK,
    "PULSE_SHADES": RUN_COLOR,
}
_GIGACODE = {
    name: globals()[name]
    for name in (
        "BG",
        "BAND_BG",
        "BLOCK_BG",
        "USER_PENDING_EDGE",
        *_DIFF,
        *_TINTED,
        *_RAMP_PEAKS,
    )
}


def _weight(color: str, anchor: str) -> float:
    """Доля anchor в color при фоне gigacode: проекция color−BG на anchor−BG."""
    bg = _rgb(_GIGACODE["BG"])
    to_anchor = [a - b for a, b in zip(_rgb(anchor), bg)]
    to_color = [c - b for c, b in zip(_rgb(color), bg)]
    dot = sum(x * y for x, y in zip(to_anchor, to_color))
    return dot / sum(x * x for x in to_anchor)


def _shift(color: str, bg: str, share: float = 1.0) -> str:
    """Сдвигает color на share сдвига фона от gigacode к bg."""
    old, new = _rgb(_GIGACODE["BG"]), _rgb(bg)
    moved = (c + share * (n - o) for c, n, o in zip(_rgb(color), new, old))
    return _hex(moved)


def _rebase(stops: list[str], peak: str, bg: str) -> list[str]:
    """Стопы рампы на новом фоне: доля фона в стопе (1 − вес пика) едет
    вместе с фоном, остальное остаётся. При фоне gigacode — ровно литералы."""
    return [_shift(stop, bg, 1 - _weight(stop, peak)) for stop in stops]


def _derived(bg: str) -> dict[str, object]:
    tinted = {
        name: _mix(bg, SURFACE_TINT, _weight(_GIGACODE[name], SURFACE_TINT))
        for name in _TINTED
    }
    ramps = {
        name: _rebase(_GIGACODE[name], peak, bg)
        for name, peak in _RAMP_PEAKS.items()
    }
    return {
        "BG": bg,
        "BAND_BG": _mix(bg, INK, 0.075),
        "BLOCK_BG": _mix(bg, INK, 0.042),
        "USER_PENDING_EDGE": _mix(bg, ACCENT, 0.64),
        **{name: _shift(_GIGACODE[name], bg) for name in _DIFF},
        **tinted,
        **ramps,
    }


# Фон терминала годится, если на нём читается текст палитры (основной и
# серый) с контрастом WCAG не ниже 3; светлые и цветные средней яркости —
# gigacode.
_TEXT = (INK, DIM)
MIN_TEXT_CONTRAST = 3.0


def apply_background(rgb: tuple[int, int, int]) -> None:
    """Тема auto: фон терминала → BG и зависящие от него токены. Вызывается
    до импорта модулей, берущих токены через `from gc_tui.palette import`."""
    globals().update(_derived(_hex(rgb)))


def reset() -> None:
    """Возврат к теме gigacode (литералы)."""
    globals().update(_GIGACODE)


def _luminance(rgb: tuple[int, int, int]) -> float:
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        for c in (channel / 255 for channel in rgb)
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(
    first: tuple[int, int, int], second: tuple[int, int, int]
) -> float:
    """Контраст двух цветов по WCAG: от 1 до 21."""
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def select_palette(
    theme: str, probe: Callable[[], tuple[int, int, int] | None]
) -> tuple[int, int, int] | None:
    """Фон, от которого строить палитру, или None — тема gigacode.

    `probe` спрашивает фон терминала и зовётся только в теме auto. Нет
    ответа или на фоне нечитаем текст палитры — gigacode.
    """
    if theme != "auto":
        return None
    rgb = probe()
    if rgb is None:
        return None
    if any(contrast(_rgb(ink), rgb) < MIN_TEXT_CONTRAST for ink in _TEXT):
        return None
    return rgb


def css_variables() -> dict[str, str]:
    """Токены → CSS-переменные (kebab-имена как в TCSS). Инжектится в
    `TuiApp.get_css_variables()`; TCSS ссылается на них через `$name`."""
    variables = {
        "bg": BG,
        "ink": INK,
        "bright": BRIGHT,
        "dim": DIM,
        "sub-ink": SUB_INK,
        "muted": MUTED,
        "deep-dim": DEEP_DIM,
        "accent": ACCENT,
        "code": CODE,
        "tool-name": TOOL_NAME,
        "code-block": CODE_BLOCK,
        "ok": OK,
        "run-color": RUN_COLOR,
        "olive": OLIVE,
        "err": ERR,
        "warn": WARN,
        "info": INFO,
        "brand": BRAND,
        "band-bg": BAND_BG,
        "block-bg": BLOCK_BG,
        "focus-bg": FOCUS_BG,
        "screen-selection-background": SCREEN_SELECTION_BACKGROUND,
        "card-select-bg": CARD_SELECT_BG,
        "frame-archive": FRAME_ARCHIVE,
        "note-ink": NOTE_INK,
        "user-band-bg": USER_BAND_BG,
        "user-pending-edge": USER_PENDING_EDGE,
        "overlay-bg": OVERLAY_BG,
        "zebra-bg": ZEBRA_BG,
        "skill": SKILL,
        "diff-add-bg": DIFF_ADD_BG,
        "diff-del-bg": DIFF_DEL_BG,
        "diff-add-hl": DIFF_ADD_HL,
        "diff-del-hl": DIFF_DEL_HL,
        "diff-gutter": DIFF_GUTTER,
        "diff-add-gutter": DIFF_ADD_GUTTER,
        "diff-del-gutter": DIFF_DEL_GUTTER,
    }
    return variables
