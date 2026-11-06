"""Раскладка реплики пользователя в ленте: перенос строк и копирование выделенного.

Реплику переносит не Textual, а эта раскладка: плашка картинки не рвётся на пробеле
внутри подписи, строки-продолжения начинаются под текстом, а не под префиксом `› `.
Раскладка рисует только готовые строки не шире виджета, поэтому Textual их больше не
переносит, и выделение приходит в координатах этих строк: номер строки и символ в ней.
По ним `copy` возвращает кусок исходного текста — без префиксов, отступов и
переносов, которые добавила раскладка. Пробел на месте переноса остаётся пробелом,
перевод строки сообщения — переводом строки.

Строка может рисоваться короче исходника (`LineSpec.shown`: путь картинки,
свёрнутый в `…`) — копия всё равно отдаёт исходник целиком.

Не покрыто: виджет уже префикса строки (меньше 7 ячеек) — строки получаются шире
виджета, Textual их дорезает.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple, Optional, Sequence

from rich.cells import cell_len, get_character_cell_size
from rich.text import Text

from gc_tui.widgets.chip_wrap import mask_chip_spaces

#: Шаг табуляции, как у Textual: таб раскрывается до следующей кратной позиции.
_TAB_SIZE = 8

_WORD = re.compile(r"\S+")


@dataclass(frozen=True)
class LineSpec:
    """Логическая строка реплики: строка сообщения или строка картинки под ним."""

    plain: str  # исходный текст — он и уходит в копию
    chips: tuple[tuple[int, int], ...]  # спаны плашек в `plain`, не рвутся
    prefix: int  # ширина префикса первой строки раскладки (символы = ячейки)
    indent: int  # отступ строк-продолжений
    # Что рисуется вместо `plain`, если короче (путь, свёрнутый в `…`): отличие —
    # один кусок, найденный по общему началу и концу; `None` — `plain`.
    shown: Optional[str] = None


class _Row(NamedTuple):
    """Строка раскладки: кусок `[start, end)` раскрытого текста своей строки."""

    line: int
    start: int
    end: int
    first: bool


class _Display(NamedTuple):
    """Рисуемая строка (табы раскрыты, `shown` подставлен) и сопоставление её
    символов с исходными."""

    text: str
    pieces: tuple[str, ...]  # что рисуется на месте каждого исходного символа
    source: tuple[int, ...]  # исходный индекс каждого символа `text` (+ конец)


def _replaced(plain: str, shown: Optional[str]) -> tuple[int, int, str]:
    """Кусок исходника `[start, end)`, который на экране заменён текстом."""
    if shown is None or shown == plain:
        return 0, 0, ""
    limit = min(len(plain), len(shown))
    head = 0
    while head < limit and plain[head] == shown[head]:
        head += 1
    tail = 0
    while tail < limit - head and plain[-1 - tail] == shown[-1 - tail]:
        tail += 1
    return head, len(plain) - tail, shown[head : len(shown) - tail]


def _display(plain: str, shown: Optional[str]) -> _Display:
    start, end, replacement = _replaced(plain, shown)
    pieces: list[str] = []
    source: list[int] = []
    column = 0
    for index, char in enumerate(plain):
        if (
            start <= index < end
        ):  # заменённый кусок — целиком на первом символе
            piece = replacement if index == start else ""
        elif char == "\t":
            piece = " " * (_TAB_SIZE - column % _TAB_SIZE)
        else:
            piece = char
        pieces.append(piece)
        source.extend([index] * len(piece))
        column += cell_len(piece)
    source.append(len(plain))
    return _Display("".join(pieces), tuple(pieces), tuple(source))


def _fold(
    text: str, start: int, end: int, avail: int, rest: int
) -> list[tuple[int, int]]:
    """Слово шире строки — куски по ширине: первый до `avail`, дальше до `rest`."""
    pieces = []
    used = 0
    for index in range(start, end):
        size = get_character_cell_size(text[index])
        if used + size > avail and index > start:
            pieces.append((start, index))
            start, used, avail = index, 0, rest
        used += size
    pieces.append((start, end))
    return pieces


def _wrap(
    text: str, chips: Sequence[tuple[int, int]], avail: int, rest: int
) -> list[tuple[int, int]]:
    """Жадный перенос по словам: плашка — одно слово (пробелы в ней замаскированы);
    пробелы на месте переноса не рисуются; слово шире строки разламывается."""
    ranges: list[tuple[int, int]] = []
    start = end = 0
    for word in _WORD.finditer(mask_chip_spaces(text, chips)):
        if cell_len(text[start : word.end()]) <= avail:
            end = word.end()
            continue
        if end > start:  # в строке уже есть слово — перенос перед этим
            ranges.append((start, end))
            start, avail = word.start(), rest
            if cell_len(text[start : word.end()]) <= avail:
                end = word.end()
                continue
        *full, (start, end) = _fold(text, start, word.end(), avail, rest)
        ranges.extend(full)
        if full:
            avail = rest
    while end < len(text) and cell_len(text[start : end + 1]) <= avail:
        end += 1  # хвостовые пробелы — пока помещаются
    ranges.append((start, end))
    return ranges


class ReplyLayout:
    """Строки раскладки реплики при заданной ширине; `width <= 0` — без переноса."""

    def __init__(self, specs: Sequence[LineSpec], width: int) -> None:
        self.specs = tuple(specs)
        self.width = width
        self._displays = [
            _display(spec.plain, spec.shown) for spec in self.specs
        ]
        rows: list[_Row] = []
        for line, (spec, shown) in enumerate(zip(self.specs, self._displays)):
            if width > 0:
                chips = [
                    (shown.source.index(a), shown.source.index(b))
                    for a, b in spec.chips
                ]
                ranges = _wrap(
                    shown.text,
                    chips,
                    max(1, width - spec.prefix),
                    max(1, width - spec.indent),
                )
            else:
                ranges = [(0, len(shown.text))]
            rows.extend(
                _Row(line, a, b, index == 0)
                for index, (a, b) in enumerate(ranges)
            )
        self._rows = tuple(rows)
        offsets = [0]
        for spec in self.specs:
            offsets.append(offsets[-1] + len(spec.plain) + 1)
        self._offsets = offsets

    @property
    def height(self) -> int:
        return len(self._rows)

    def render(self, prefixes: Sequence[Text], bodies: Sequence[Text]) -> Text:
        """Отрисовка: `prefixes` — префиксы первых строк (ширина `LineSpec.prefix`),
        `bodies` — оформленный исходный текст строк (стили и meta плашек)."""
        shown = [
            _expand(body, display)
            for body, display in zip(bodies, self._displays)
        ]
        out = Text()
        for index, row in enumerate(self._rows):
            if index:
                out.append("\n")
            spec = self.specs[row.line]
            out.append_text(
                prefixes[row.line] if row.first else Text(" " * spec.indent)
            )
            out.append_text(shown[row.line][row.start : row.end])
        return out

    def copy(
        self,
        start: Optional[tuple[int, int]],
        end: Optional[tuple[int, int]],
    ) -> str:
        """Исходный текст под выделением; `start`/`end` — `(x, y)` Textual:
        символ в строке раскладки и номер строки, `None` — от начала / до конца."""
        full = "\n".join(spec.plain for spec in self.specs)
        first = 0 if start is None else self._source_index(start, at_end=False)
        last = (
            len(full) if end is None else self._source_index(end, at_end=True)
        )
        return full[first:last]

    def _source_index(self, offset: tuple[int, int], *, at_end: bool) -> int:
        x, y = offset
        if y >= len(self._rows):
            return self._offsets[-1] - 1
        row = self._rows[y]
        spec = self.specs[row.line]
        column = x - (spec.prefix if row.first else spec.indent)
        if column <= 0 and at_end and y > 0:
            # Конец выделения в префиксе — ничего из этой строки не выделено.
            row = self._rows[y - 1]
            return self._global(row, row.end)
        return self._global(
            row, row.start + min(max(column, 0), row.end - row.start)
        )

    def _global(self, row: _Row, index: int) -> int:
        return self._offsets[row.line] + self._displays[row.line].source[index]


def _expand(body: Text, display: _Display) -> Text:
    """Оформленная рисуемая строка: подменённые символы берут стили исходного."""
    if display.text == body.plain:
        return body
    out = Text()
    done = 0
    for index, piece in enumerate(display.pieces):
        if piece != body.plain[index]:
            out.append_text(body[done:index])
            start = len(out)
            out.append(piece)
            for span in body[index : index + 1].spans:
                out.stylize(span.style, start, len(out))
            done = index + 1
    out.append_text(body[done:])
    return out
