"""Пилюля режима разрешений в футере — презентация.

Единственный постоянный индикатор режима. Цвет — только на тексте: рамка поля
ввода занята событиями (вопрос агента), второй цветовой сигнал там конкурировал
бы с первым. Глифы: ⏵⏵ — агент правит без остановки, ⏸ — остановится и спросит,
☰ — составляет план и ничего не меняет, ⚡ — работает автономно, спорное
проверяется автоматически.
"""

from __future__ import annotations

from gc_core.security.permission_mode import DEFAULT_MODE, mode_label
from rich.text import Text

from gc_tui.palette import ACCENT, DEEP_DIM, DIM, INK, RUN_COLOR

GLYPH_PAUSE = "⏸"
GLYPH_PLAY = "⏵⏵"
GLYPH_PLAN = "☰"
GLYPH_AUTO = "⚡"

#: Глиф и цвет — презентация, живут здесь; ИМЯ режима — ``mode_label`` из
#: ``gc_core.security.permission_mode``: одно имя на весь продукт.
_STYLES = {
    "edit": (GLYPH_PLAY, INK),
    "manual": (GLYPH_PAUSE, DEEP_DIM),
    "plan": (GLYPH_PLAN, ACCENT),
    "auto": (GLYPH_AUTO, RUN_COLOR),
}

#: Подсказка хоткея — постоянная подпись к режиму, а не одноразовый совет: она
#: объясняет, чем переключается то, что стоит слева от неё. Скобки привязывают
#: её к имени режима; разделитель поставил бы её в ряд с соседями по футеру.
_HINT = " (Shift+Tab)"

#: Зазор между пилюлей и командами, стоящими за ней в футере.
PILL_GAP = "  "

#: Транзиентная подсказка при смене режима — ТОЛЬКО описание, без имени: имя
#: стоит в пилюле слева. Ключ нужен на каждый собранный режим (стережёт тест):
#: технического фолбэка нет намеренно — он раскрывал бы внутреннее имя.
MODE_HINTS = {
    "edit": "агент редактирует файлы, опасные команды через подтверждение",
    "manual": "подтверждение всех действий, кроме чтения",
    "plan": "только чтение: агент исследует и предлагает план",
    "auto": "все действия без подтверждения, опасное блокируется",
}


def mode_color(mode: str) -> str:
    """Цвет режима — тот же, что у пилюли футера (ползунок Настроек тоже)."""
    return _STYLES.get(mode, _STYLES[DEFAULT_MODE.value])[1]


def render_mode_pill(
    mode: str, *, narrow: bool = False, hint: bool = True
) -> Text:
    """Глиф + имя режима + подсказка хоткея (незнакомый режим → режим по умолчанию).

    Ширина — по содержимому: пилюля не добивается пробелами про запас.

    narrow — только глиф, без имени и подсказки (для совсем узкого терминала).
    hint=False — без « (Shift+Tab)»: для транзиентной подсказки, где место
    важнее отдать самому сообщению.
    """
    key = mode if mode in _STYLES else DEFAULT_MODE.value
    glyph, style = _STYLES[key]
    out = Text()
    if narrow:
        out.append(glyph, style=style)
        return out
    out.append(f"{glyph} {mode_label(key)}", style=style)
    if hint:
        out.append(_HINT, style=DIM)
    return out
