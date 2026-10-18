"""Обновление Static только при изменении содержимого.

Каждый ``Static.update`` с ``layout=True`` перекладывает экран; хелпер
пропускает обновление, если новое содержимое равно текущему.
"""

from rich.text import Text
from textual.widgets import Static


def same_content(old: object, new: object) -> bool:
    """Равны ли содержимое двух обновлений Static.

    ``Text.__eq__`` сравнивает только ``plain`` и ``spans``, поэтому
    базовый стиль и параметры вывода сверяются здесь. Смешанные типы и
    неизвестные объекты считаются изменившимися.
    """
    if isinstance(old, Text) and isinstance(new, Text):
        return (
            old.plain == new.plain
            and old.spans == new.spans
            and old.style == new.style
            and old.justify == new.justify
            and old.overflow == new.overflow
            and old.no_wrap == new.no_wrap
            and old.end == new.end
        )
    if isinstance(old, str) and isinstance(new, str):
        return old == new
    return False


def update_if_changed(widget: Static, content: object) -> None:
    """Обновить ``widget``, если содержимое отличается от текущего."""
    if not same_content(widget.content, content):
        widget.update(content)
