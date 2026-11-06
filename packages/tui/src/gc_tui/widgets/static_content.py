"""Обновление Static только при изменении содержимого.

Каждый ``Static.update`` с ``layout=True`` перекладывает экран; хелпер
пропускает обновление, если новое содержимое равно текущему.
"""

from rich.text import Text
from textual.widgets import Static


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


def update_if_changed(widget: Static, content: object) -> None:
    """Обновить ``widget``, если содержимое отличается от текущего."""
    if not same_content(widget.content, content):
        widget.update(content)
