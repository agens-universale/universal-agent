"""Строка вызова всегда в одну строку: режется только объект.

Подпись, хвост (таймер, сводка итога, счётчики правки) и ❐ не режутся и не
переносятся; объект между подписью и хвостом сокращается `…` под ширину
строки — команда с конца, путь серединой (`truncate_path`)."""

from __future__ import annotations

import json

import pytest

_CMD = (
    "cd /Users/someone/dev/sber/claude-remote/wt-p3-trace && "
    "ls .superpowers/sdd/2026-10-21-p3a-trace-look/ && cat a.md"
)
_PATH = (
    "packages/tui/src/gc_tui/widgets/very/deep/nested/folder/structure/"
    "and/even/more/levels/of/nesting/here/"
    "tool_call_row.py"
)
_WIDTHS = (60, 80, 120)


def _first_line(row) -> str:
    return str(row.query_one(".trace-content").render())


def _line_height(row) -> int:
    from textual.containers import Horizontal

    return row.query_one(Horizontal).size.height


def _app(build):
    from textual.app import App
    from textual.containers import VerticalScroll

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            build(self.query_one("#t", VerticalScroll))

    return _A()


def _row(wire_name: str, target: str, level: int = 0):
    from gc_tui.services.trace_render import build_call_row

    return build_call_row(
        call_id="c1", wire_name=wire_name, args_summary=target, level=level
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("width", _WIDTHS)
@pytest.mark.parametrize("level", (0, 1))
async def test_running_long_command_keeps_timer_on_one_line(width, level):
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(
        lambda t: t.mount(_row("exec_command", _CMD, level))
    ).run_test(size=(width, 20)) as pilot:
        await pilot.pause()
        row = pilot.app.query_one(ToolCallRow)
        line = _first_line(row)
        assert line.startswith("Run  cd /Users/")
        assert line.endswith(" · 0 с")
        assert "…" in line
        assert _line_height(row) == 1
        assert row.size.height == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("width", _WIDTHS)
async def test_finished_row_keeps_result_tail(width):
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(lambda t: t.mount(_row("exec_command", _CMD))).run_test(
        size=(width, 20)
    ) as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="188 строк", outcome="ok")
        await pilot.pause()
        line = _first_line(row)
        assert line.endswith("… · 188 строк")
        assert _line_height(row) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("width", _WIDTHS)
async def test_edit_row_keeps_counts_and_copy_button(width):
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(
        lambda t: t.mount(_row("fs_apply_file_edits", _PATH))
    ).run_test(size=(width, 20)) as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="", outcome="ok")
        row.set_edit_counts(12, 3, copy_text="diff")
        await pilot.pause()
        line = _first_line(row)
        # путь серединой в `…`: имя файла цело
        assert line.startswith("Edit  packages/")
        assert line.endswith("…/tool_call_row.py +12 −3")
        assert row.query_one(".block-copy").display
        assert _line_height(row) == 1


@pytest.mark.asyncio
async def test_failed_row_consequence_wraps_below_one_line_call():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(lambda t: t.mount(_row("exec_command", _CMD))).run_test(
        size=(60, 20)
    ) as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="exit 2: нет такого файла", outcome="error")
        await pilot.pause()
        assert _line_height(row) == 1
        assert row.size.height == 2


@pytest.mark.asyncio
async def test_resize_refits_target():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(lambda t: t.mount(_row("exec_command", _CMD))).run_test(
        size=(120, 20)
    ) as pilot:
        row = pilot.app.query_one(ToolCallRow)
        await pilot.pause()
        wide = _first_line(row)
        await pilot.resize_terminal(60, 20)
        await pilot.pause()
        narrow = _first_line(row)
        assert len(narrow) < len(wide)
        assert narrow.endswith(" · 0 с")
        assert _line_height(row) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("width", _WIDTHS)
async def test_preview_row_fits_one_line(width):
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import render_segments_readonly
    from gc_tui.widgets.tool_call_row import ToolCallRow

    segments = [
        {
            "index": 0,
            "messages": [
                {
                    "ts": "",
                    "payload": {
                        "type": "function_call",
                        "id": "fc1",
                        "call_id": "p1",
                        "name": "exec_command",
                        "arguments": json.dumps({"command": _CMD}),
                    },
                },
                {
                    "ts": "",
                    "payload": {
                        "type": "function_call_output",
                        "id": "fo1",
                        "call_id": "p1",
                        "output": json.dumps({"total_lines": 188}),
                    },
                },
            ],
            "compacted_by": None,
        }
    ]
    async with _app(lambda t: render_segments_readonly(t, segments)).run_test(
        size=(width, 20)
    ) as pilot:
        await pilot.pause()
        row = pilot.app.query_one(ToolCallRow)
        line = _first_line(row)
        assert line.startswith("Run  cd /Users/")
        assert line.endswith("… · 188 строк")
        assert _line_height(row) == 1
