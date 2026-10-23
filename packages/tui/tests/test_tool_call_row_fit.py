"""Строка вызова всегда в одну строку.

Подпись, таймер, счётчики правки и ❐ не режутся и не переносятся; объект
между подписью и хвостом сокращается `…` под ширину строки — команда с
конца, путь серединой (`truncate_path`). Длинный свободный текст хвоста
(сводка итога, живой статус) режется `…`, когда объект дошёл до минимума."""

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
        assert line.endswith(" · 0с")
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
        assert line.endswith("…/tool_call_row.py (+12 −3)")
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
        assert narrow.endswith(" · 0с")
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
        # Превью — через модель блоков, как живая лента: `Ran` (P4 §4).
        assert line.startswith("Ran  cd /Users/")
        assert line.endswith("… · 188 строк")
        assert _line_height(row) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("width", _WIDTHS)
@pytest.mark.parametrize("elapsed", (0, 6, 3725))
async def test_running_tail_not_under_scrollbar(tmp_path, width, elapsed):
    """С полосой прокрутки хвост ` · N с` виден целиком левее полосы:
    ширина под объект считается от content-колонки, полоса в неё не входит."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.duration_format import format_duration
    from textual.containers import VerticalScroll

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test(size=(width, 24)) as pilot:
        trace = pilot.app.screen.query_one("#trace", VerticalScroll)
        for i in range(40):
            done = _row("exec_command", f"ls {i}")
            done.set_result(summary="1 строка", outcome="ok")
            trace.mount(done)
        row = _row("exec_command", _CMD)
        trace.mount(row)
        await pilot.pause()
        row._start_monotonic -= elapsed
        row._update()
        trace.scroll_end(animate=False)
        await pilot.pause()
        assert trace.show_vertical_scrollbar
        visible = trace.scrollable_content_region
        content = row.query_one(".trace-content").region
        assert content.right <= visible.right
        strip = pilot.app.screen._compositor.render_strips()[content.y]
        line = strip.text[visible.x : visible.right]
        assert line.rstrip().endswith(f"… · {format_duration(elapsed)}")
        # Полная строка не прижата к полосе: справа есть хотя бы одна клетка.
        assert line.endswith(" ")


_LONG_TAIL = "совпадений в файлах проекта " * 6


def _shown_target(line: str, label: str) -> str:
    return line[len(label) + 2 :].split(" · ", 1)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("width", (40, 60, 80))
async def test_long_result_tail_is_cut_not_wrapped(width):
    """Длинный хвост итога не переносит строку и не съедает объект: объект
    режется первым, но не короче своего минимума; хвост — с `…`."""
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import CALL_TARGET_MIN_COLS
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(lambda t: t.mount(_row("exec_command", _CMD))).run_test(
        size=(width, 20)
    ) as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary=_LONG_TAIL, outcome="ok")
        await pilot.pause()
        line = _first_line(row)
        assert _line_height(row) == 1
        assert line.endswith("…")
        assert " · совпадений" in line
        assert len(_shown_target(line, "Run")) >= CALL_TARGET_MIN_COLS


@pytest.mark.asyncio
async def test_long_live_status_keeps_timer():
    """Живой статус длинный — режется он, таймер хвоста цел."""
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import CALL_TARGET_MIN_COLS
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(lambda t: t.mount(_row("exec_command", _CMD))).run_test(
        size=(60, 20)
    ) as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_live_status(_LONG_TAIL)
        await pilot.pause()
        line = _first_line(row)
        assert _line_height(row) == 1
        assert line.endswith("… · 0с")
        assert len(_shown_target(line, "Run")) >= CALL_TARGET_MIN_COLS


_TASK = (
    "Review the changes in client/retry.py for correctness and edge "
    "cases, check the retry table against the policy and report every "
    "mismatch with a short reason and the line it is on"
)


def _agent_row(task: str):
    from gc_tui.models import AgentHead
    from gc_tui.services.trace_render import build_call_row

    return build_call_row(
        call_id="a1",
        wire_name="subagent_code_reviewer",
        args_summary=task,
        level=0,
        agent=AgentHead("Reviewer", task),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("width", (40, 60, 80))
async def test_agent_task_line_two_rows_then_ellipsis(width):
    """Задача субагента — `└` под заголовком: не больше двух строк,
    дальше `…`; заголовок — роль в одну строку."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(lambda t: t.mount(_agent_row(_TASK))).run_test(
        size=(width, 20)
    ) as pilot:
        await pilot.pause()
        row = pilot.app.query_one(ToolCallRow)
        assert _first_line(row).startswith("Agent  Reviewer")
        assert row.size.height == 3
        body = str(row.query_one(".consequence-body").render())
        assert body.startswith("Review the changes")
        assert body.endswith("…")
        assert len(body.split("\n")) == 2


@pytest.mark.asyncio
async def test_agent_short_task_one_row():
    pytest.importorskip("textual")
    from gc_tui.palette import DIM
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(lambda t: t.mount(_agent_row("проверь клиент"))).run_test(
        size=(80, 20)
    ) as pilot:
        await pilot.pause()
        row = pilot.app.query_one(ToolCallRow)
        assert row.size.height == 2
        body = str(row.query_one(".consequence-body").render())
        assert body == "проверь клиент"
        assert row._consequence_bodies()[0].style == DIM
