"""Строка списка с колонками: голова (имя, статус, источник) не переносится,
хвост переносится в своей колонке — продолжение встаёт под хвост, а не под
имя (дизайн-код §10).

Хвост — последовательность неразрывных кусков («light qwen3 ·»): перенос
только между ними, ведущие пробелы куска в начале строки срезаются. Кусок
шире колонки складывается внутри неё. Колонка уже `_MIN_COLUMN` — обычный
перенос всей строки: лесенка из пары символов читается хуже."""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Console, ConsoleOptions, RenderResult
from rich.measure import Measurement
from rich.text import Text

_MIN_COLUMN = 12


class ColumnRow:
    def __init__(self, head: Text, atoms: Sequence[Text]) -> None:
        self.head = head
        self.atoms = list(atoms)

    @property
    def plain(self) -> str:
        return self.head.plain + "".join(a.plain for a in self.atoms)

    def __str__(self) -> str:
        return self.plain

    def _plain_text(self) -> Text:
        return Text.assemble(self.head, *self.atoms)

    def _flow(self, width: int) -> list[Text]:
        """Куски хвоста по строкам ширины ``width``."""
        lines: list[Text] = []
        line = Text()
        for atom in self.atoms:
            if line and line.cell_len + atom.cell_len > width:
                line.rstrip()
                lines.append(line)
                line = Text()
            if not line:
                lead = len(atom.plain) - len(atom.plain.lstrip())
                atom = atom[lead:]
            line.append_text(atom)
        lines.append(line)
        return lines

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        indent = self.head.cell_len
        column = options.max_width - indent
        if not self.atoms or column < _MIN_COLUMN:
            yield self._plain_text()
            return
        tail: list[Text] = []
        for line in self._flow(column):
            tail.extend(line.wrap(console, column))
        out = Text("\n").join(
            [self.head + tail[0]]
            + [Text(" " * indent) + line for line in tail[1:]]
        )
        out.no_wrap = True
        yield out

    def __rich_measure__(
        self, console: Console, options: ConsoleOptions
    ) -> Measurement:
        return Measurement.get(console, options, self._plain_text())


class ColumnRows:
    """Блок строк одной ``Static`` — пути `/config`, тулы карточки MCP:
    каждая ``ColumnRow`` переносится в своей колонке, ``Text`` — как есть."""

    def __init__(self, rows: Sequence[ColumnRow | Text]) -> None:
        self.rows = list(rows)

    @property
    def plain(self) -> str:
        return "\n".join(row.plain for row in self.rows)

    def __str__(self) -> str:
        return self.plain

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield from self.rows

    def __rich_measure__(
        self, console: Console, options: ConsoleOptions
    ) -> Measurement:
        return Measurement.get(console, options, Text(self.plain))
