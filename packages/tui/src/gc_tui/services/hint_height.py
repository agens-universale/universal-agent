"""Резерв высоты подсказки под выбором (дизайн-код §9): подсказка, что
меняется от курсора или выбранного значения, не сдвигает раскладку —
высота под самый длинный вариант при текущей ширине.

Перенос считает Rich — тот же движок, что рисует `Static`: оценка по
`textwrap` расходится с ним на краю строки, а занижение и есть прыжок."""

from __future__ import annotations

import io
from collections.abc import Iterable

from rich.console import Console
from rich.text import Text


def wrapped_lines(text: str, width: int) -> int:
    """Строк, которые займёт ``text`` в ``width`` клеток (переносы строк
    в тексте — тоже); ширины ещё нет или текст пуст — одна строка."""
    if width < 1 or not text:
        return 1
    console = Console(width=width, file=io.StringIO())
    return max(1, len(Text(text).wrap(console, width)))


def reserved_lines(texts: Iterable[str], width: int) -> int:
    """Строк под подсказку — по самому длинному из вариантов ``texts``."""
    return max((wrapped_lines(t, width) for t in texts), default=1)
