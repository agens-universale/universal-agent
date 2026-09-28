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
        "focus-bg": FOCUS_BG,
        "screen-selection-background": SCREEN_SELECTION_BACKGROUND,
        "card-select-bg": CARD_SELECT_BG,
        "frame-archive": FRAME_ARCHIVE,
    }
    return variables
