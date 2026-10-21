"""Следствия `└` под своим событием и хвосты результата (спека P3 §2.1, §3).

Ошибка вызова — строка `└` внутри виджета вызова (на +2 от глифа), хвост
успеха — ` · сводка` по-русски; вопрос без ответа и прерванный вопрос —
строка `└` внутри рамки вопроса. Каждый случай — вживую и в превью."""

from __future__ import annotations

import json

import pytest
from gc_tui.models import TraceEvent
from gc_tui.palette import DIM, ERR
from gc_tui.services.trace_mapper import map_message
from gc_tui.services.trace_render import (
    _dispatch_readonly,
    consequence_text,
    merge_nested_readonly,
)
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.trace_row import QuestionRow, TraceRow
from rich.text import Text


def _styles_of(text: Text, fragment: str) -> list[str]:
    return [
        str(s.style)
        for s in text.spans
        if fragment in text.plain[s.start : s.end]
    ]


# --- consequence_text --------------------------------------------------------


def test_consequence_text_sits_two_columns_right_of_glyph():
    line = consequence_text(0, Text("тело"))
    assert line.plain == "  └ тело"
    assert _styles_of(line, "└") == [DIM]


def test_consequence_text_nested_level_is_two_deeper():
    assert consequence_text(1, Text("x")).plain == "    └ x"


# --- хвосты результата -------------------------------------------------------


def _result(data: dict) -> str:
    msg = {
        "type": "function_call_output",
        "name": "fs_read_file",
        "call_id": "c1",
        "output": json.dumps(data),
    }
    ev = map_message(msg, no=1, ts="")
    return ev.summary


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"total_lines": 188}, "188 строк"),
        ({"total_lines": 1}, "1 строка"),
        ({"lines": 2}, "2 строки"),
        ({"matches": list(range(14))}, "14 совпадений"),
        ({"matches": 1}, "1 совпадение"),
        ({"matches": 3}, "3 совпадения"),
        ({"chars": 1200}, "1200 символов"),
        ({"chars": 21}, "21 символ"),
        ({"entries": ["a", "b"]}, "2 записи"),
        ({"entries": ["a"], "count": 5}, "5 записей"),
        ({"count": 12}, "12 записей"),
        ({"bytes": 2}, "2 байта"),
        ({"bytes": 5}, "5 байт"),
        ({"exit": 2}, "код выхода 2"),
        ({"source": "cache"}, "источник cache"),
    ],
)
def test_result_tail_in_russian(data, expected):
    assert _result(data) == expected


def test_result_tail_bits_joined_with_middle_dot():
    assert _result({"total_lines": 5, "chars": 40}) == "5 строк · 40 символов"


# --- строка вызова: хвост и ошибка -------------------------------------------


def _consequence(row: ToolCallRow) -> str:
    return row._render_consequence().plain


def test_ok_row_tail_is_middle_dot_summary():
    row = ToolCallRow(call_id="c1", label="Read", target="x")
    row.set_result(summary="40 строк", error=False)
    content = row._render_content()
    assert content.plain == "Read  x · 40 строк"
    assert _styles_of(content, "40 строк") == [DIM]
    assert _consequence(row) == ""
    assert not row.has_class("has-consequence")


def test_error_row_has_no_tail_and_error_line_below():
    row = ToolCallRow(call_id="c1", label="Edit", target="x")
    row.set_result(summary="search не найден в файле", error=True)
    assert row._render_content().plain == "Edit  x"
    assert "✗" in row._render_prefix().plain
    line = row._render_consequence()
    assert line.plain == "  └ search не найден в файле"
    assert _styles_of(line, "search не найден") == [ERR]
    assert row.has_class("has-consequence")


def test_nested_error_line_two_deeper():
    row = ToolCallRow(call_id="c1", label="Edit", target="x", level=1)
    row.set_result(summary="нет файла", error=True)
    assert _consequence(row) == "    └ нет файла"


def test_add_consequence_appends_line_under_row():
    row = ToolCallRow(call_id="c1", label="View", target="a.png")
    row.set_result(summary="", error=False)
    row.add_consequence(Text("! пометка"))
    assert _consequence(row) == "  └ ! пометка"
    assert row.has_class("has-consequence")


@pytest.mark.asyncio
async def test_consequence_line_renders_below_row_and_copy_stays_right():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    app = TuiApp()
    async with app.run_test(size=(80, 30)) as pilot:
        trace = pilot.app.screen.query_one("#trace", VerticalScroll)
        row = ToolCallRow(call_id="c1", label="Edit", target="x")
        trace.mount(row)
        row.set_edit_counts(1, 1, copy_text="d")
        row.set_result(summary="boom", error=True)
        await pilot.pause()
        line = row.query_one(".trace-consequence", Static)
        copy = row.query_one(".block-copy", Static)
        content = row.query_one(".trace-content", Static)
        assert line.display
        assert line.region.y == content.region.y + 1
        assert line.region.x == row.region.x
        assert copy.region.y == content.region.y
        assert copy.region.right == row.region.right


def test_running_tail_timer_has_space_before_unit():
    import time

    row = ToolCallRow(call_id="c1", label="Agent", target="")
    row._start_monotonic = time.monotonic() - 4.0
    assert row._render_content().plain.endswith(" · 4 с")


# --- превью: ошибка вызова и вложенные ---------------------------------------


class _Target:
    def __init__(self):
        self.children: list = []

    def mount(self, widget, after=None):
        if after is None:
            self.children.append(widget)
        else:
            self.children.insert(self.children.index(after) + 1, widget)


def _ev(kind: str, **kw) -> TraceEvent:
    base = {
        "no": 1,
        "event_ts_utc_iso": "",
        "kind": kind,
        "title": "",
        "summary": "",
        "full_payload": {},
        "message_id": None,
    }
    base.update(kw)
    return TraceEvent(**base)


def test_preview_error_is_line_under_call_not_tail():
    target, calls = _Target(), {}
    _dispatch_readonly(
        target,
        _ev(
            "call",
            call_id="c1",
            wire_name="fs_read_file",
            tool_name="fs_read_file",
            args_summary="x",
        ),
        calls,
    )
    _dispatch_readonly(
        target,
        _ev("error", call_id="c1", summary="search не найден в файле"),
        calls,
    )
    (row,) = target.children
    assert row._render_content().plain == "Read  x"
    assert _consequence(row) == "  └ search не найден в файле"


def test_preview_next_nested_call_goes_after_error_line():
    """Следствие — внутри строки вызова: якорь следующего вложенного —
    сама строка, а не середина между строкой и её `└`."""
    target, calls = _Target(), {}
    parent = ToolCallRow(call_id="p1", label="Agent", target="")
    target.mount(parent)
    calls["p1"] = parent
    records = [
        _sidecar(1, "call", {"call_id": "n1", "name": "fs_edit_file"}),
        _sidecar(2, "error", {"call_id": "n1", "output": "boom"}),
        _sidecar(3, "call", {"call_id": "n2", "name": "fs_read_file"}),
    ]
    merge_nested_readonly(target, records, calls)
    first, second = target.children[1], target.children[2]
    assert first.call_id == "n1" and second.call_id == "n2"
    assert _consequence(first) == "    └ boom"


def _sidecar(seq: int, kind: str, payload: dict) -> dict:
    return {
        "parent_call_id": "p1",
        "seq": seq,
        "kind": kind,
        "payload": payload,
    }


# --- вживую: ошибка вложенного вызова ----------------------------------------


@pytest.mark.asyncio
async def test_live_next_nested_call_goes_after_error_line():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.containers import VerticalScroll

    class _LiveRunner:
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        parent = ToolCallRow(call_id="p1", label="Agent", target="")
        trace.mount(parent)
        scr._active_calls["p1"] = parent
        await pilot.pause()
        scr._mount_nested_tool(
            parent_call_id="p1",
            tool_name="fs_edit_file",
            tool_call_id="n1",
            args_summary="x",
        )
        scr._finish_nested_tool(
            parent_call_id="p1", tool_call_id="n1", ok=False, error_brief="e"
        )
        scr._mount_nested_tool(
            parent_call_id="p1",
            tool_name="fs_read_file",
            tool_call_id="n2",
            args_summary="y",
        )
        await pilot.pause()
        rows = [w for w in trace.children if isinstance(w, ToolCallRow)]
        assert [r.call_id for r in rows] == ["p1", "n1", "n2"]
        assert _consequence(rows[1]) == "    └ e"


# --- вопрос без ответа -------------------------------------------------------


def _ask_records(call_id: str = "ah1") -> list[dict]:
    return [
        {
            "id": "q1",
            "type": "function_call",
            "name": "ask_human",
            "call_id": call_id,
            "arguments": json.dumps({"question": "Подтвердите?"}),
        },
        {
            "id": "q2",
            "type": "function_call_output",
            "name": "ask_human",
            "call_id": call_id,
            "output": json.dumps(
                {
                    "tool": "ask_human",
                    "ok": True,
                    "response": "(empty response)",
                }
            ),
        },
    ]


def test_preview_unanswered_question_one_frame_with_line():
    from gc_tui.services.trace_render import render_segments_readonly

    target = _Target()
    render_segments_readonly(
        target,
        [{"messages": [{"payload": p} for p in _ask_records()]}],
    )
    (question,) = target.children
    assert isinstance(question, QuestionRow)
    assert str(question._content) == "? Подтвердите?\n  └ без ответа"


@pytest.mark.asyncio
async def test_live_unanswered_question_one_frame_with_line(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        for i, payload in enumerate(_ask_records(), start=1):
            scr._render_event(map_message(payload, no=i, ts=""))
        await pilot.pause()
        questions = list(scr.query(QuestionRow))
        assert len(questions) == 1
        assert str(questions[0]._content) == ("? Подтвердите?\n  └ без ответа")
        texts = [str(w._content) for w in scr.query(TraceRow)]
        assert not any("спрашивал" in t for t in texts)
