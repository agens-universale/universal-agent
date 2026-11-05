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
# Инлайн-код / путь / тех. имя (тулы, сервера, ключи) — голубой, как у
# `SKILL` (решение владельца 2026-11-01; был сиреневый #afbdff).
CODE = "#afd7ff"
TOOL_NAME = "#BCC3CC"
CODE_BLOCK = "#9aa4b2"
OK = "#5FAE7F"
# Амбер работы: тон и светлота прежнего #D4A574, насыщенность OKLCH 0.103
# вместо 0.085 — рядом с OK/ERR прежний выглядел выцветшим (решение
# владельца 2026-11-05). Тон не сдвинут: к жёлтому амбер желтит.
RUN_COLOR = "#dba265"
OLIVE = "#8FA05A"  # тип в подсветке /view; был серединой градиента бренда
# Ближайший к прежнему #C75C54 красный, при котором ошибка на блоке
# вывода проходит WCAG 4.5 (было 4.21); на глаз почти тот же.
ERR = "#cd615a"
WARN = "#f0883e"  # предупреждение (не ошибка): контекст-метр, ⚠-уведомления
INFO = "#58a6ff"  # ℹ-уведомления (info-уровень)
BRAND = "#9A6DD7"

# Плашки футера (контекст-метр, голосовая пилюля)
TRACK = "#3f4654"  # «трек» незаполненной части метра: серый над тёмным футером
TEXT_ON_FILL = "#ffffff"  # текст поверх цветной заливки плашек (метр, пилюля)

# Стопы градиента бренда: логотип, шкала контекста, голосовая плашка. Свои
# токены — правка цветов интерфейса (амбер работы, ACCENT, OLIVE) бренд не
# сдвигает. Тон и светлота — от амбера, оливы и ACCENT; насыщенность у всех
# трёх одна, OKLCH 0.097: края градиента одинаково плотные, чуть ярче прежних
# (прежние стопы #D4A574, #8FA05A, #54B3A6 — 0.085…0.096; 0.103 вышло
# слишком насыщенно; решение владельца 2026-11-05).
GRADIENT_WARM = "#d9a36a"
GRADIENT_MID = "#8fa059"
GRADIENT_COOL = "#4eb4a6"

# Поверхности
BAND_BG = "#1f2128"
# Блочный контент ленты (код, таблицы, диффы) — тише полосы баннеров: блок
# должен читаться карточкой, но не «выпирать» из почти чёрного трейса.
# Дистанция до BG: 15.6 против 27.7 у BAND_BG (метрика _distance тестов палитры).
BLOCK_BG = "#171a22"

# Выделение текста мышью. Тот же синий, которым Textual выделяет текст в
# TextArea (экран /buffer): #368AE9 с прозрачностью 40% поверх фона даёт
# примерно это значение — интерфейс говорит про выделение одним цветом.
#
# Почему не светлее: комментарии в подсветке кода тёмные, и на более светлом
# фоне их контраст падает ниже читаемого. Пара «этот фон + комментарии на $dim»
# (см. services/code_render) подобрана так, чтобы читаемым осталось всё.
# Границы закреплены тестами в packages/tui/tests/test_palette_sync.py.
SCREEN_SELECTION_BACKGROUND = "#1e416c"
# Выбранная карточка (/sessions, /skills, /mcp): мягче USER_BAND_BG,
# светлее BAND_BG.
CARD_SELECT_BG = "#2e323d"
FRAME_ARCHIVE = "#4b525d"  # серая рамка «архив/история» (отвеченный вопрос)
NOTE_INK = "#a5adb8"  # ремарка: чуть светлее DIM
# Подложка реплики. Светлее BAND_BG, темнее HIGHLIGHT_BG.
USER_BAND_BG = "#363b47"
# Выбранная строка списков и пикеров (палитра, /diff, настройки,
# онбординг, /mcp). Свой токен: от подложки реплики и подсветки ленты не
# зависит, значение пока то же, что у USER_BAND_BG.
LIST_SELECT_BG = "#363b47"
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


# Диффы правок агента: фоны строк — тёмные чистые зелёный и тёплый красный
# (выбор владельца по референсу 2026-11-05; запасной — примесь OK/ERR к BG
# 0.36). Примесь OK/ERR к BG давала бирюзовый и сиреневатый оттенки и
# выглядела блекло. Intraline-акцент — фон строки с примесью 0.35 своего
# семантического цвета (OK #5FAE7F / ERR #C75C54): тон строки сохраняется,
# слово светлее. Закреплён литералами — правка OK/ERR не сдвигает дифф.
# Дистанции (метрика тестов палитры, порог различимости 40): строки add/del
# ≈64.4, строка/фон ≈46.4 / 67.5, акцент/строка ≈55.9 / 51.0.
# В теме auto фоны диффа сдвигаются вместе с фоном терминала (_DIFF, _derived).
DIFF_ADD_BG = "#183b2a"
DIFF_DEL_BG = "#501f1b"
DIFF_ADD_HL = "#316348"
DIFF_DEL_HL = "#7a342f"
DIFF_GUTTER = MUTED  # колонка номеров строк — существующий приглушённый токен
# «Добавлено / удалено» — один цвет везде: счётчики `+N −M` (шапки вызовов,
# экраны правок) и номер с маркером строки диффа. Свои сочные зелёный и
# красный, светлее семантических OK/ERR (выбор владельца 2026-11-05): ERR на
# красной заливке строки читался с контрастом WCAG 3.3 — ниже нормы мелкого
# текста. Насыщенность важнее светимости: примеси к MUTED и к INK отвергнуты
# на смоуке 2026-08-10-11 как выцветшие. Красный — той же насыщенности, что
# зелёный (OKLCH C ≈0.19): светлее он уходит в бледно-лососевый.
DIFF_ADD_INK = "#56d364"
DIFF_DEL_INK = "#fe6558"
# Номер строки и маркер +/- красятся по ТИПУ строки ОДНИМ цветом: структура
# правки считывается по колонке слева, не читая код. Свои токены, потому что
# лежат на заливке строки и в теме auto едут с ней (_DIFF); в gigacode — те
# же DIFF_ADD_INK / DIFF_DEL_INK.
#
# Границы (метрики тестов палитры):
# * контраст WCAG на своей строке 6.4 / 4.65 (норма 4.5); на фоне терминала
#   заливка светлее, худший случай стражей — 3.98 (порог там 3);
# * дистанция до заливки строки 174.1 / 197.2, до фона блока у контекстной
#   строки — 206.3 / 248.8 против 158.3 у DIFF_GUTTER (порог различимости 40);
# * пара add/del расходится на 201.2.
DIFF_ADD_GUTTER = DIFF_ADD_INK
DIFF_DEL_GUTTER = DIFF_DEL_INK

# Отказ вызова в ленте (глиф `○`): приглушённый оранжевый — заметен, но не
# спорит с ошибкой и предупреждением. В теме auto — от фона (_derived).
_REFUSED_SHARE = 0.55
REFUSED = _mix(BG, WARN, _REFUSED_SHARE)

# Реплика в очереди (ждёт впуска ядром): подложка тише обычной — между фоном
# ленты и USER_BAND_BG. В теме auto — от пересчитанных фона и USER_BAND_BG.
_QUEUE_SHARE = 0.5
QUEUE_BG = _mix(BG, USER_BAND_BG, _QUEUE_SHARE)

# «Тихая реплика»: один вид у реплики в очереди и у липкой реплики над
# лентой. Подложка — QUEUE_BG; текст — DIM; маркер — MUTED; имя скилла —
# цветом текста (отдельного токена нет); ховер липкой — HIGHLIGHT_BG.
# В теме auto — от пересчитанного QUEUE_BG (_derived).
QUIET_REPLY_INK = DIM
QUIET_REPLY_MARK = MUTED
# Текст липкой реплики бледнее, чем у реплики в очереди (решение 32):
# подложка и маркер общие, ink — свой.
STICKY_REPLY_INK = DEEP_DIM

# Подсветка: фокус и выделение строки ленты, ховер накладок (липкая реплика
# и плашка «в конец») — один фон, светлее USER_BAND_BG; текст не меняется
# (у накладок — исключение из ховера `$accent`). В теме auto — от
# пересчитанного USER_BAND_BG.
_HIGHLIGHT_SHARE = 0.12
HIGHLIGHT_BG = _mix(USER_BAND_BG, INK, _HIGHLIGHT_SHARE)


# Рампы анимаций (ping-pong, пик — токен палитры); значения те же, что были
# литералами в виджетах. В теме auto их пересчитывает apply_background.
# Дыхание «думаю»: gold, пик #f7c688.
GLOW_SHADES = [
    "#a2783b",
    "#be8f4b",
    RUN_COLOR,
    "#e7b377",
    "#f3c389",
    "#f7c688",
    "#f3c389",
    "#e7b377",
    RUN_COLOR,
    "#be8f4b",
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


# ── Тема auto: палитра от фона терминала ───────────────────────────────────
# Всё выше — тема gigacode (литералы, снапшоты неизменны). apply_background
# подменяет фон и зависящие от него токены; reset возвращает литералы.

# Тон синеватых поверхностей (подложка реплики, карточки, оверлеи, трек
# метра). Один параметр: "#ffffff" даёт нейтральные поверхности той же
# светлоты. Подобран так, что при фоне gigacode каждая поверхность
# расходится с литералом <= 2.
SURFACE_TINT = "#78829b"
_TINTED = (
    "CARD_SELECT_BG",
    "USER_BAND_BG",
    "LIST_SELECT_BG",
    "OVERLAY_BG",
    "ZEBRA_BG",
    "TRACK",
)
# Цвета диффа едут вместе с фоном на тот же сдвиг: их взаимные дистанции
# (стражи различимости add/del, акцент/строка, строка/фон) не зависят от
# фона. Примесь OK/ERR к самому фону на светлом фоне теряла их ниже порога.
# Номер и маркер едут с заливкой строки: иначе на светлом фоне их контраст
# на ней падал ниже 4.5.
_DIFF = (
    "DIFF_ADD_BG",
    "DIFF_DEL_BG",
    "DIFF_ADD_HL",
    "DIFF_DEL_HL",
    "DIFF_ADD_GUTTER",
    "DIFF_DEL_GUTTER",
)
# Рампа → её пик (сам пик от фона не зависит).
_RAMP_PEAKS = {
    "GLOW_SHADES": "#f7c688",
    "PROCESSING_SHADES": DEEP_DIM,
    "VALIDATING_SHADES": CODE_BLOCK,
}
_GIGACODE = {
    name: globals()[name]
    for name in (
        "BG",
        "BAND_BG",
        "BLOCK_BG",
        "REFUSED",
        "QUEUE_BG",
        "HIGHLIGHT_BG",
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
        "REFUSED": _mix(bg, WARN, _REFUSED_SHARE),
        **{name: _shift(_GIGACODE[name], bg) for name in _DIFF},
        **tinted,
        "QUEUE_BG": _mix(bg, tinted["USER_BAND_BG"], _QUEUE_SHARE),
        "HIGHLIGHT_BG": _mix(tinted["USER_BAND_BG"], INK, _HIGHLIGHT_SHARE),
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
        "gradient-warm": GRADIENT_WARM,
        "gradient-mid": GRADIENT_MID,
        "gradient-cool": GRADIENT_COOL,
        "err": ERR,
        "warn": WARN,
        "refused": REFUSED,
        "info": INFO,
        "brand": BRAND,
        "band-bg": BAND_BG,
        "block-bg": BLOCK_BG,
        "screen-selection-background": SCREEN_SELECTION_BACKGROUND,
        "card-select-bg": CARD_SELECT_BG,
        "frame-archive": FRAME_ARCHIVE,
        "note-ink": NOTE_INK,
        "user-band-bg": USER_BAND_BG,
        "list-select-bg": LIST_SELECT_BG,
        "queue-bg": QUEUE_BG,
        "highlight-bg": HIGHLIGHT_BG,
        "overlay-bg": OVERLAY_BG,
        "zebra-bg": ZEBRA_BG,
        "skill": SKILL,
        "quiet-reply-ink": QUIET_REPLY_INK,
        "quiet-reply-mark": QUIET_REPLY_MARK,
        "diff-add-bg": DIFF_ADD_BG,
        "diff-del-bg": DIFF_DEL_BG,
        "diff-add-hl": DIFF_ADD_HL,
        "diff-del-hl": DIFF_DEL_HL,
        "diff-gutter": DIFF_GUTTER,
        "diff-add-ink": DIFF_ADD_INK,
        "diff-del-ink": DIFF_DEL_INK,
        "diff-add-gutter": DIFF_ADD_GUTTER,
        "diff-del-gutter": DIFF_DEL_GUTTER,
    }
    return variables
