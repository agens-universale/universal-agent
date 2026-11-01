"""ReservedHint — подсказка под выбором с высотой под самый длинный
вариант (дизайн-код §9): ход курсором или смена значения меняет только
текст, раскладка ниже стоит на месте.

Текущий текст входит в резерв всегда: подсказка, что собирается на ходу
(или временно занятый слот — вопрос подтверждения), не обрезается, а
один раз растит высоту.

Высота ставится уже при монтировании — по ширине родителя (экраны
пересобирают тело на каждый ход, и свежая подсказка без раскладки иначе
мелькнула бы своей высотой); со своей шириной — пересчёт в `on_resize`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from rich.text import Text
from textual import events
from textual.widget import Widget
from textual.widgets import Static

from gc_tui.services.hint_height import reserved_lines


class ReservedHint(Static):
    """Подсказка: ``variants`` — все тексты, что она может показать на
    этом экране (строки, значения), не только текущий."""

    def __init__(
        self,
        content: object = "",
        *,
        variants: Callable[[], Iterable[str]],
        **kwargs,
    ) -> None:
        super().__init__(content, **kwargs)
        self._variants = variants
        self._current = _plain(content)

    def update(self, content: object = "", *, layout: bool = True) -> None:
        super().update(content, layout=layout)
        self._current = _plain(content)
        if self.is_mounted:
            self._reserve()

    def on_mount(self) -> None:
        self._reserve()

    def on_resize(self, _event: events.Resize) -> None:
        self._reserve()

    def reserved_height(self) -> int | None:
        """Высота под самый длинный вариант (с вертикальным паддингом) при
        текущей ширине; ширины ещё нет — None."""
        width = self._text_width()
        if width <= 0:
            return None
        texts = [*self._variants(), self._current]
        padding = self.styles.padding
        return reserved_lines(texts, width) + padding.top + padding.bottom

    def _reserve(self) -> None:
        height = self.reserved_height()
        if height is not None:
            self.styles.height = height

    def _text_width(self) -> int:
        """Ширина текста: своя (без паддинга), а до раскладки — ширина
        содержимого родителя минус свой горизонтальный паддинг."""
        own = self.content_region.width
        if own > 0:
            return own
        parent = self.parent
        if not isinstance(parent, Widget):
            return 0
        padding = self.styles.padding
        return parent.content_region.width - padding.left - padding.right


def _plain(content: object) -> str:
    """Текст содержимого для подсчёта строк."""
    return content.plain if isinstance(content, Text) else str(content)
