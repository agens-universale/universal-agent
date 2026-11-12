"""Строка с колонками: хвост переносится в своей колонке, под собой."""

import io

from gc_tui.services.column_row import ColumnRow, ColumnRows
import pytest
from rich.console import Console
from rich.text import Text
from textual.geometry import Offset


def _lines(row, width: int) -> list[str]:
    console = Console(width=width, file=io.StringIO(), color_system=None)
    with console.capture() as cap:
        console.print(row)
    return [line.rstrip() for line in cap.get().splitlines()]


def _row() -> ColumnRow:
    return ColumnRow(
        Text("  work      глобальный   "),
        [Text("light aa ·"), Text(" default bbbb ·"), Text(" max cc")],
    )


def test_fits_on_one_line_as_plain_text():
    assert _lines(_row(), 80) == [
        "  work      глобальный   light aa · default bbbb · max cc"
    ]


def test_wraps_under_its_own_column_by_whole_atoms():
    head = len("  work      глобальный   ")
    assert _lines(_row(), head + 22) == [
        "  work      глобальный   light aa ·",
        " " * head + "default bbbb · max cc",
    ]


def test_atom_longer_than_column_folds_inside_column():
    row = ColumnRow(Text("ab  "), [Text("x" * 40)])
    assert _lines(row, 20) == [
        "ab  " + "x" * 16,
        "    " + "x" * 16,
        "    " + "x" * 8,
    ]


def test_too_narrow_for_column_falls_back_to_plain_wrap():
    row = ColumnRow(Text("abcdefgh  "), [Text("one two")])
    assert _lines(row, 12) == ["abcdefgh", "one two"]


def test_str_is_the_plain_row():
    assert str(_row()) == (
        "  work      глобальный   light aa · default bbbb · max cc"
    )


def test_no_atoms_is_just_the_head():
    assert _lines(ColumnRow(Text("  без профиля"), []), 40) == [
        "  без профиля"
    ]


def test_rows_block_wraps_each_row_in_its_column():
    block = ColumnRows(
        [
            Text("title"),
            ColumnRow(Text("a:  "), [Text("/very/long/path/to/file")]),
            ColumnRow(Text("bb: "), [Text("short")]),
        ]
    )
    assert _lines(block, 20) == [
        "title",
        "a:  /very/long/path/",
        "    to/file",
        "bb: short",
    ]
    assert block.plain == "title\na:  /very/long/path/to/file\nbb: short"


def _block() -> ColumnRows:
    return ColumnRows(
        [
            Text("title"),
            ColumnRow(Text("a:  "), [Text("/very/long/path/to/file")]),
            ColumnRow(Text("bb: "), [Text("short")]),
        ]
    )


def _app(widget):
    from textual.app import App

    class _A(App):
        def compose(self):
            yield widget

    return _A()


@pytest.mark.asyncio
async def test_static_keeps_columns_and_copies_wrapped_lines():
    """Строки с колонками в `ColumnStatic`: перенос в своей колонке, а
    выделение мышью отдаёт текст ровно тех строк, что на экране."""
    from gc_tui.widgets.column_static import ColumnStatic
    from textual.selection import Selection

    widget = ColumnStatic(_block())
    async with _app(widget).run_test(size=(20, 10)) as pilot:
        await pilot.pause()
        assert widget.size.height == 4
        everything = widget.get_selection(Selection(None, None))
        assert everything == (
            "title\na:  /very/long/path/\n    to/file\nbb: short",
            "\n",
        )
        tail = Selection(Offset(4, 2), Offset(11, 2))
        assert widget.get_selection(tail) == ("to/file", "\n")


@pytest.mark.asyncio
async def test_static_with_plain_text_selects_as_usual():
    from gc_tui.widgets.column_static import ColumnStatic
    from textual.selection import Selection

    widget = ColumnStatic(Text("просто текст"))
    async with _app(widget).run_test(size=(40, 5)) as pilot:
        await pilot.pause()
        assert widget.get_selection(Selection(None, None)) == (
            "просто текст",
            "\n",
        )
