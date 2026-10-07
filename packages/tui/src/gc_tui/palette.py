"""Цветовые токены TUI — единый источник правды для CSS и Python-рендера.

Здесь определяются ВСЕ цвета. `styles.tcss` их не дублирует: `TuiApp.get_css_variables()`
инжектит `css_variables()` в набор CSS-переменных, поэтому `$code`, `$ok`, … в TCSS
резолвятся из этого модуля. Код, красящий через `rich.Text(..., style=...)`, берёт те
же константы напрямую. Одно значение — один источник.

Имена констант соответствуют TCSS-переменным (`$deep-dim` → `DEEP_DIM`,
`$run-color` → `RUN_COLOR`, ...).
"""

from __future__ import annotations

# Базовые
BG = "#0E1119"
INK = "#ebe9e6"
BRIGHT = "#eef2f6"
DIM = "#8b949e"
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
# Границы закреплены тестами в tests/tui/test_palette_sync.py.
SCREEN_SELECTION_BACKGROUND = "#1e416c"
CARD_SELECT_BG = "#2e323d"  # выбранная карточка (/sessions, /skills, /mcp) — мягче focus-bg, светлее band-bg
FRAME_ARCHIVE = "#4b525d"  # серая рамка «архив/история» (отвеченный вопрос)


def _mix(base: str, accent: str, t: float) -> str:
    """Смешивает accent с base в пропорции t∈[0,1] → hex (base при t=0, accent при t=1)."""
    b = [int(base.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    a = [int(accent.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    mixed = (round(bc + (ac - bc) * t) for bc, ac in zip(b, a))
    return "#" + "".join(f"{c:02x}" for c in mixed)


# Диффы правок агента: фоны строк — примесь OK/ERR к BG (не «чистые» red/green,
# дифф остаётся в гамме интерфейса), intraline-акценты — более насыщенная примесь
# того же цвета, рисуется поверх фона строки. Коэффициент 0.29 — минимум, при
# котором ADD_BG и DEL_BG расходятся на дистанцию >=40 (порог неразличимости
# двух тёмных поверхностей в этом проекте, см. test_selection_distinguishable_from_focus):
# при 0.29 дистанция ≈41.3, при 0.4 — уже ≈56.1. Старое значение 0.15 (коэффициент
# ФОНОВ в другой паре, selection/focus, до их разведения) дало бы здесь только
# ≈21.5 — под порогом. Коэффициент HL поднят вслед за фоном (0.5, дистанция ≈70.4),
# чтобы акцент остался различим над новым, более насыщенным фоном строки.
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


def css_variables() -> dict[str, str]:
    """Токены → CSS-переменные (kebab-имена как в TCSS). Инжектится в
    `TuiApp.get_css_variables()`; TCSS ссылается на них через `$name`."""
    variables = {
        "bg": BG,
        "ink": INK,
        "bright": BRIGHT,
        "dim": DIM,
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
        "diff-add-bg": DIFF_ADD_BG,
        "diff-del-bg": DIFF_DEL_BG,
        "diff-add-hl": DIFF_ADD_HL,
        "diff-del-hl": DIFF_DEL_HL,
        "diff-gutter": DIFF_GUTTER,
        "diff-add-gutter": DIFF_ADD_GUTTER,
        "diff-del-gutter": DIFF_DEL_GUTTER,
    }
    return variables
