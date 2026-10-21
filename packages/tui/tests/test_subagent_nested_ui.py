"""MainScreen: batch waiting/active + drain nested subagent progress."""

from __future__ import annotations

import pytest
from gc_core.execution.progress import (
    QueueSubagentProgressSink,
    SubagentProgressEvent,
)
from gc_tui.widgets.tool_call_row import ToolCallRow


@pytest.mark.asyncio
async def test_batch_calls_first_active_rest_waiting():
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
        a = ToolCallRow(call_id="a", label="t1", target="", waiting=True)
        b = ToolCallRow(call_id="b", label="t2", target="", waiting=True)
        c = ToolCallRow(call_id="c", label="t3", target="", waiting=True)
        for row in (a, b, c):
            trace.mount(row)
            scr._active_calls[row.call_id] = row
        await pilot.pause()
        scr._reconcile_active_tool_phases()
        await pilot.pause()
        assert "●" in str(a._render_prefix())
        assert "•" in str(b._render_prefix())
        assert "прервано" not in str(b._render_content())
        assert "•" in str(c._render_prefix())


@pytest.mark.asyncio
async def test_drain_mounts_nested_under_parent():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.containers import VerticalScroll

    class _LiveRunner:
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    sink = QueueSubagentProgressSink()
    app._subagent_progress_sink = sink

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        parent = ToolCallRow(
            call_id="parent_1",
            label="subagent_worker",
            target="task=x",
        )
        trace.mount(parent)
        scr._active_calls["parent_1"] = parent
        await pilot.pause()

        sink.emit(
            SubagentProgressEvent(
                parent_call_id="parent_1",
                child_run_id="child",
                kind="tool_start",
                step=2,
                max_steps=50,
                elapsed_ms=0,
                tool_name="fs_read_file",
                tool_call_id="tc_nested",
                args_summary="path='a.py'",
            )
        )
        scr._drain_subagent_progress()
        await pilot.pause()

        nested = scr._nested_tool_rows.get("tc_nested")
        assert nested is not None
        assert nested._level == 1
        content = str(nested._render_content())
        assert content.startswith("Read  path='a.py'")
        assert "fs_read_file" not in content
        # heartbeat на корне
        assert "step 2/50" in str(parent._render_content())


@pytest.mark.asyncio
async def test_nested_stays_after_parent_result():
    """После finish + result parent nested остаётся под ● (живая сессия)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from textual.containers import VerticalScroll

    class _LiveRunner:
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    sink = QueueSubagentProgressSink()
    app._subagent_progress_sink = sink

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        parent = ToolCallRow(
            call_id="parent_1",
            label="subagent_worker",
            target="task=x",
        )
        trace.mount(parent)
        scr._active_calls["parent_1"] = parent
        await pilot.pause()

        sink.emit(
            SubagentProgressEvent(
                parent_call_id="parent_1",
                child_run_id="child",
                kind="tool_start",
                step=1,
                max_steps=50,
                elapsed_ms=0,
                tool_name="fs_read_file",
                tool_call_id="tc_nested",
                args_summary="path='a.py'",
            )
        )
        sink.emit(
            SubagentProgressEvent(
                parent_call_id="parent_1",
                child_run_id="child",
                kind="tool_end",
                step=1,
                max_steps=50,
                elapsed_ms=5,
                tool_name="fs_read_file",
                tool_call_id="tc_nested",
                ok=True,
            )
        )
        sink.emit(
            SubagentProgressEvent(
                parent_call_id="parent_1",
                child_run_id="child",
                kind="finished",
                step=1,
                max_steps=50,
                elapsed_ms=10,
                ok=True,
            )
        )
        scr._drain_subagent_progress()
        await pilot.pause()

        scr._render_event(
            TraceEvent(
                no=1,
                event_ts_utc_iso="",
                kind="result",
                title="",
                summary="ok · 1 step",
                full_payload={},
                call_id="parent_1",
            )
        )
        await pilot.pause()

        nested = scr._nested_tool_rows.get("tc_nested")
        assert nested is not None
        assert nested in scr._nested_widgets_by_parent.get("parent_1", [])
        assert "parent_1" not in scr._active_calls
        assert parent._result is not None
        assert "●" in str(parent._render_prefix())


# --- живой вид = перерисовка из записи субагента -----------------------------
#
# Один вывод тула (или одна промежуточная реплика) даёт и событие прогресса
# ядра, и запись sidecar. Живой путь, возврат в сессию (plan/apply на экране)
# и превью (`merge_nested_readonly`) рисуют одно и то же.

import json  # noqa: E402

from gc_core.execution.progress import (  # noqa: E402
    parse_tool_result_status,
    result_facts,
    tool_args_summary,
)
from gc_tui.palette import DIM, ERR, OK, REFUSED  # noqa: E402

_ARGS = json.dumps({"path": "a.txt", "content": "x"})

_CALL_CASES = {
    "ok": (None, "●", OK, []),
    "step_limit": (
        json.dumps({"ok": False, "status": "step_limit"}),
        "○",
        DIM,
        ["лимит шагов"],
    ),
    "cancelled": (
        json.dumps(
            {
                "ok": False,
                "status": "cancelled_by_user",
                "error": {"code": "cancelled_by_user", "message": "stop"},
            }
        ),
        "○",
        DIM,
        ["отменено"],
    ),
    "refused": (
        json.dumps(
            {
                "ok": False,
                "error": {
                    "type": "write_denied_by_mode",
                    "mode": "plan",
                    "reason": "x",
                },
            }
        ),
        "○",
        REFUSED,
        ["режим plan: запись запрещена"],
    ),
    "error": (
        json.dumps(
            {"ok": False, "error": {"type": "x", "message": "нет файла"}}
        ),
        "●",
        ERR,
        ["нет файла"],
    ),
}

_PROSE_CASES = {
    "accepted": ("проверю", True, None, []),
    "rejected": ("проверю", False, "нет плана", ["· не принят — нет плана"]),
    "rejected_bare": ("проверю", False, None, ["· не принят"]),
    "rejected_empty": ("", False, "нет плана", None),
}


def _live_call_events(pid: str, case: str) -> list:
    output = _CALL_CASES[case][0]
    base = dict(
        parent_call_id=pid, child_run_id="c", step=1, max_steps=5,
        elapsed_ms=0, tool_name="fs_write_file", tool_call_id=f"{pid}_n",
    )  # fmt: skip
    ok, brief = parse_tool_result_status(output) if output else (True, None)
    status, fields = result_facts(output) if ok is False else (None, None)
    return [
        SubagentProgressEvent(
            kind="tool_start",
            args_summary=tool_args_summary("fs_write_file", _ARGS),
            **base,
        ),
        SubagentProgressEvent(
            kind="tool_end",
            ok=ok,
            error_brief=brief,
            result_status=status,
            result_error=fields,
            **base,
        ),
    ]


def _call_records(pid: str, case: str) -> list:
    output = _CALL_CASES[case][0]
    call = {"call_id": f"{pid}_n", "name": "fs_write_file", "arguments": _ARGS}
    end_kind = "result" if output is None else "error"
    end = {"call_id": f"{pid}_n", "output": output or "ok"}
    return [
        {"parent_call_id": pid, "seq": 1, "kind": "call", "payload": call},
        {"parent_call_id": pid, "seq": 2, "kind": end_kind, "payload": end},
    ]


def _live_prose_events(pid: str, case: str) -> list:
    prose, ok, brief, _lines = _PROSE_CASES[case]
    return [
        SubagentProgressEvent(
            parent_call_id=pid, child_run_id="c", kind="intermediate_reply",
            step=1, max_steps=5, elapsed_ms=0, prose=prose, ok=ok,
            error_brief=brief,
        )
    ]  # fmt: skip


def _prose_records(pid: str, case: str) -> list:
    prose, ok, brief, _lines = _PROSE_CASES[case]
    payload: dict = {"prose": prose, "call_id": f"{pid}_ir"}
    if ok is False:
        payload["ok"] = False
        if brief:
            payload["error_brief"] = brief
    return [
        {
            "parent_call_id": pid,
            "seq": 1,
            "kind": "intermediate_reply",
            "payload": payload,
        }
    ]


def _view(widget) -> tuple:
    """Что видно в ленте: глиф со стилем, текст, строки `└` со стилем."""
    from gc_tui.widgets.trace_row import ConsequenceTraceRow

    if isinstance(widget, ToolCallRow):
        return (
            "call",
            widget._render_prefix(),
            widget._render_content().plain,
            widget._consequence_bodies(),
        )
    assert isinstance(widget, ConsequenceTraceRow)
    return ("prose", widget._prefix, widget._content, widget._consequences)


class _Target:
    def __init__(self):
        self.children: list = []

    def mount(self, widget, after=None):
        if after is None:
            self.children.append(widget)
        else:
            self.children.insert(self.children.index(after) + 1, widget)


def _preview_views(records: list, pid: str) -> list:
    from gc_tui.services.trace_render import merge_nested_readonly

    target = _Target()
    parent = ToolCallRow(call_id=pid, label="Agent", target="")
    target.mount(parent)
    merge_nested_readonly(target, records, {pid: parent})
    return [_view(w) for w in target.children[1:]]


_ALL = [("call", c) for c in _CALL_CASES] + [
    ("prose", c) for c in _PROSE_CASES
]
_EVENTS = {"call": _live_call_events, "prose": _live_prose_events}
_RECORDS = {"call": _call_records, "prose": _prose_records}


@pytest.mark.asyncio
async def test_live_nested_outcome_equals_rebuild_and_preview():
    """Для каждого исхода: вживую = возврат в сессию = превью."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.subagent_sidecar_mapper import (
        apply_sidecar_nested_actions,
        plan_sidecar_nested_actions,
    )
    from textual.containers import VerticalScroll

    class _LiveRunner:
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    sink = QueueSubagentProgressSink()
    app._subagent_progress_sink = sink
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        for what, case in _ALL:
            for side in ("live", "rebuild"):
                pid = f"{side}_{what}_{case}"
                parent = ToolCallRow(call_id=pid, label="Agent", target="")
                trace.mount(parent)
                scr._active_calls[pid] = parent
        await pilot.pause()
        for what, case in _ALL:
            for ev in _EVENTS[what](f"live_{what}_{case}", case):
                sink.emit(ev)
            apply_sidecar_nested_actions(
                plan_sidecar_nested_actions(
                    _RECORDS[what](f"rebuild_{what}_{case}", case)
                ),
                mount_nested_tool=scr._mount_nested_tool,
                finish_nested_tool=scr._finish_nested_tool,
                mount_nested_prose=scr._mount_nested_prose,
            )
        scr._drain_subagent_progress()
        await pilot.pause()

        for what, case in _ALL:
            live = [
                _view(w)
                for w in scr._nested_widgets_by_parent.get(
                    f"live_{what}_{case}", []
                )
            ]
            rebuilt = [
                _view(w)
                for w in scr._nested_widgets_by_parent.get(
                    f"rebuild_{what}_{case}", []
                )
            ]
            preview = _preview_views(
                _RECORDS[what](f"rebuild_{what}_{case}", case),
                f"rebuild_{what}_{case}",
            )
            assert live == rebuilt == preview, (what, case)
            _assert_look(what, case, live)


def _assert_look(what: str, case: str, views: list) -> None:
    if what == "call":
        _output, glyph, style, lines = _CALL_CASES[case]
        ((_kind, prefix, _content, bodies),) = views
        assert prefix.plain.strip() == glyph
        assert [
            str(s.style)
            for s in prefix.spans
            if glyph in prefix.plain[s.start : s.end]
        ] == [style]
        assert [b.plain for b in bodies] == lines
        return
    lines = _PROSE_CASES[case][3]
    if lines is None:
        assert views == []
        return
    ((_kind, prefix, content, bodies),) = views
    assert (prefix.strip(), content) == ("●", "проверю")
    assert [(b.plain, str(b.style)) for b in bodies] == [
        (line, DIM) for line in lines
    ]


@pytest.mark.asyncio
async def test_rejected_nested_prose_line_under_its_reply():
    """Строка `└` — внутри виджета реплики, на +2 от её глифа."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.consequence_line import ConsequenceLine
    from textual.containers import VerticalScroll

    class _LiveRunner:
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    sink = QueueSubagentProgressSink()
    app._subagent_progress_sink = sink
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        parent = ToolCallRow(call_id="p1", label="Agent", target="")
        trace.mount(parent)
        scr._active_calls["p1"] = parent
        await pilot.pause()
        for ev in _live_prose_events("p1", "rejected"):
            sink.emit(ev)
        scr._drain_subagent_progress()
        await pilot.pause()
        (row,) = scr._nested_widgets_by_parent["p1"]
        (line,) = row.query(ConsequenceLine)
        assert line.text.plain == "    └ · не принят — нет плана"
