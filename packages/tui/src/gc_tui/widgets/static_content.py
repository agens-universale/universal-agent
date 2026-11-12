"""Обновление Static только при изменении содержимого.

Каждый ``Static.update`` с ``layout=True`` перекладывает экран; хелпер
пропускает обновление, если новое содержимое равно текущему, а новое
содержимое того же размера обновляет без перекладки.
"""

import re

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import Static

from gc_tui import textual_compat


def text_key(text: Text | None) -> tuple | None:
    """Неизменяемый ключ вида ``text``: всё, что влияет на вывод.

    ``Text.__eq__`` сравнивает только ``plain`` и ``spans``; базовый стиль
    и параметры вывода тоже меняют картинку, поэтому входят в ключ."""
    if text is None:
        return None
    return (
        text.plain,
        tuple(text.spans),
        str(text.style),
        text.justify,
        text.overflow,
        text.no_wrap,
        text.end,
    )


def same_content(old: object, new: object) -> bool:
    """Равны ли содержимое двух обновлений Static.

    Rich ``Text`` сравнивается по ``text_key``. Смешанные типы и
    неизвестные объекты считаются изменившимися.
    """
    if isinstance(old, Text) and isinstance(new, Text):
        return text_key(old) == text_key(new)
    if isinstance(old, str) and isinstance(new, str):
        return old == new
    return False


def _layout_view(content: object, markup: bool) -> tuple[str, tuple] | None:
    """Текст и параметры вывода, от которых зависит размер; ``None`` —
    размер по содержимому не определить."""
    if isinstance(content, Text):
        params = (
            content.no_wrap,
            content.overflow,
            content.justify,
            content.tab_size,
        )
        return content.plain, params
    if isinstance(content, str) and not markup:
        return content, ()
    return None


# Куски строки, как их меряет перенос Textual: слово целиком или один
# пробельный символ. Ширина графемы из нескольких кодовых точек (ZWJ,
# селектор вида) — не сумма ширин её точек.
_CHUNK = re.compile(r"\S+|\s")


def _line_shape(line: str) -> tuple[int, tuple[int, ...]]:
    """Ширина строки в ячейках и позиции её пробельных символов."""
    cells = 0
    spaces = []
    for chunk in _CHUNK.finditer(line):
        text = chunk.group()
        if text.isspace():
            spaces.append(cells)
        cells += cell_len(text)
    return cells, tuple(spaces)


def _shape(plain: str) -> tuple[tuple[int, tuple[int, ...]], ...]:
    return tuple(_line_shape(line) for line in plain.split("\n"))


def same_size(old: object, new: object, *, markup: bool) -> bool:
    """Займёт ли ``new`` в Static ровно место ``old`` — консервативно.

    Только пара ``Text``/``Text`` или ``str``/``str`` при ``markup=False``:
    то же число строк, та же ширина в ячейках каждой строки, те же позиции
    пробельных символов в ячейках (тогда перенос по словам совпадает), у
    ``Text`` те же ``no_wrap``, ``overflow``, ``justify``, ``tab_size``.
    Табуляция, прочие типы и строка с разметкой — ``False``.

    Разные типы (``Text``/``str``) — ``False``. Ширина — по кускам
    (слово, пробельный символ) целиком, как у Rich и Textual.

    Неучтённый остаток: символы разной ширины при равной сумме ячеек
    (``"字a"`` против ``"a字"``) — перенос по символам в узком окне может
    разойтись, а размер посчитан равным.
    """
    if type(old) is not type(new):
        return False
    old_view = _layout_view(old, markup)
    new_view = _layout_view(new, markup)
    if old_view is None or new_view is None:
        return False
    (old_plain, old_params), (new_plain, new_params) = old_view, new_view
    if "\t" in old_plain or "\t" in new_plain:
        return False
    return old_params == new_params and _shape(old_plain) == _shape(new_plain)


def update_if_changed(widget: Static, content: object) -> None:
    """Обновить ``widget``, если содержимое отличается от текущего; того
    же размера (``same_size``) — без перекладки экрана."""
    current = widget.content
    if same_content(current, content):
        return
    markup = textual_compat.static_markup(widget)
    widget.update(
        content, layout=not same_size(current, content, markup=markup)
    )
