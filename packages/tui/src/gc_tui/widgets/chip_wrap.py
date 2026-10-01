"""Перенос строк поля ввода, не разрывающий плашки.

Textual переносит строку на любом пробельном символе, в том числе неразрывном, а в
подписях плашек (`[Изображение #N]`, `[Вставка #N · K строк]`) есть пробелы. Расчёт
переноса (`WrappedDocument`) и навигатор курсора получают вместо документа поля эту
обёртку: в её строках пробелы внутри плашек заменены непробельным символом той же
ширины. Длины и ширины строк совпадают с настоящими, поэтому смещения переноса и
позиции курсора верны, а рисует поле по-прежнему настоящий текст.

Опирается на внутреннее устройство Textual: расчёт переноса и навигатор читают
строки документа через `lines`, `get_line`, `[i]` и `line_count`, остальное обёртка
отдаёт от документа как есть. Сторож — `packages/tui/tests/test_chip_wrap.py`.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

#: Непробельный символ шириной в одну ячейку — как пробел, который он заменяет.
_MASK = "_"

#: Спаны плашек текущего текста поля: строка → [(начало, конец), …].
ChipSpans = Callable[[], Mapping[int, Iterable[tuple[int, int]]]]


def mask_chip_spaces(line: str, spans: Iterable[tuple[int, int]]) -> str:
    """Строка, где пробельные символы внутри плашек заменены `_MASK`."""
    chars = list(line)
    for start, end in spans:
        for index in range(start, end):
            if chars[index].isspace():
                chars[index] = _MASK
    return "".join(chars)


class ChipWrapDocument:
    """Документ поля для расчёта переноса: строки с «несрываемыми» плашками."""

    def __init__(self, document: Any, chip_spans: ChipSpans) -> None:
        self._document = document
        self._chip_spans = chip_spans

    @property
    def lines(self) -> list[str]:
        spans = self._chip_spans()
        return [
            mask_chip_spaces(line, spans.get(row, ()))
            for row, line in enumerate(self._document.lines)
        ]

    def get_line(self, index: int) -> str:
        return mask_chip_spaces(
            self._document.get_line(index), self._chip_spans().get(index, ())
        )

    def __getitem__(self, index: int) -> str:
        return self.get_line(index)

    @property
    def line_count(self) -> int:
        return self._document.line_count

    def __getattr__(self, name: str) -> Any:
        return getattr(self._document, name)
