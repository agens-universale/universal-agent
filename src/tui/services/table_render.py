"""Рендер markdown-таблицы в rich.Text заданной ширины.

Зачем не rich.Table напрямую: Table — RichVisual, координаты выделения на него
не отображаются (тот же контракт, что для кода — см. services/code_render).
Здесь таблица раскладывается один раз в текст со стилями, и дальше ведёт себя
как обычный текст.

Ширина обязательна и не выводится: раскладка Table зависит от неё, поэтому
владелец виджета пересобирает текст при изменении размера.
"""
from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.text import Text


def table_to_text(headers: list[str], rows: list[list[str]],
                  aligns: list[str], *, width: int) -> Text:
    """Таблица как Text: псевдографика и стили сохранены, хвостового \\n нет."""
    from tui.services.markdown_render import build_markdown_table

    table = build_markdown_table(headers, rows, aligns)
    console = Console(file=StringIO(), width=max(width, 1))
    text = Text()
    lines = console.render_lines(table, pad=False)
    for index, line in enumerate(lines):
        for segment in line:
            text.append(segment.text, segment.style)
        if index < len(lines) - 1:
            text.append("\n")
    text.rstrip()
    return text
