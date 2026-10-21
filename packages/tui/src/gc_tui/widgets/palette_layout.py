"""Геометрия строк overlay-списков над вводом (`CommandPalette`,
`SkillPicker`): ширина контента до раскладки, колонка имён скиллов с
потолком и усечение текста многоточием. Одно правило на оба оверлея —
колонки и обрезка у них совпадают.
"""

from __future__ import annotations

from textual._context import NoActiveAppError
from textual.widget import Widget

SKILL_NAME_MAX = 32  # потолок общей колонки имён, клетки


def skill_short(skill: dict) -> str:
    """Короткое имя скилла — часть id после `:` (у плоского — весь id)."""
    return str(skill.get("id", "")).rpartition(":")[2]


def skill_name_key(skill: dict) -> tuple[str, str]:
    """Ключ алфавитного порядка скиллов: короткое имя без регистра, при
    равенстве — полный id. Порядок реестра (слои) в оверлее не виден и
    выглядел бы случайным."""
    return skill_short(skill).lower(), str(skill.get("id", ""))


def fit_text(text: str, width: int) -> str:
    """Текст не шире `width` клеток; длиннее — с многоточием в конце."""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def fit_name(label: str) -> str:
    """Имя длиннее `SKILL_NAME_MAX` клеток — с многоточием в конце."""
    return fit_text(label, SKILL_NAME_MAX)


def name_col_w(labels: list[str]) -> int:
    """Ширина колонки имён: самое длинное видимое имя, не шире
    `SKILL_NAME_MAX`, + 3 пробела; нет имён — 0."""
    if not labels:
        return 0
    return min(max(map(len, labels)), SKILL_NAME_MAX) + 3


def overlay_content_width(widget: Widget) -> int:
    """Ширина контента оверлея (за вычетом рамки и колонки полосы
    прокрутки), известная ДО первого layout-прохода: `self.size` у ещё не
    уложенного виджета — Size(0, 0), а оверлей всегда занимает всю ширину
    `#root` (= ширина экрана, см. `_mount_input_overlay`), поэтому ширина
    берётся из `app.size`. Вне приложения (юнит-тесты без активного `App`)
    ширина неизвестна — 0."""
    try:
        app_width = widget.app.size.width
    except NoActiveAppError:
        return 0
    # -2 — рамка .palette (round border); минус колонка вертикальной
    # полосы прокрутки — всегда, есть полоса или нет: появится она после
    # раскладки, и строка, посчитанная без неё, срезалась бы на хвосте.
    return app_width - 2 - widget.styles.scrollbar_size_vertical
