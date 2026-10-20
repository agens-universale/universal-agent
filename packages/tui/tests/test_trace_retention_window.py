"""Payload-retention: tools вне segments[-2:] light, prose всегда полный.

Спека 2026-09-25. Диск и export не клипаем — проверяем RAM/DOM TUI.
"""

from __future__ import annotations

from gc_testkit.tui_support import tui_settings
import json

import pytest
from gc_tui.services.footer_slot import RETENTION_DETAIL_HINT
from gc_tui.services.trace_retention import (
    events_leaving_window,
    filter_sidecar_to_parents,
    message_ids_in_retention_window,
    parent_outside_retention,
    payload_mode,
    retention_window_start_index,
    subagent_parent_ids_in_window,
)

_HEAVY = "HEAVY_TOOL_BODY_" + ("x" * 3000)
_OLD_USER = "OLD_USER_FULL_" + ("ю" * 180)
_OLD_ASSISTANT = "OLD_ASSISTANT_FULL_" + ("а" * 180)
_OLD_FINISH = "OLD_FINISH_FULL_" + ("ф" * 180)
_MID_USER = "MID_USER_FULL_" + ("м" * 80)
_LIVE_USER = "LIVE_USER_FULL_" + ("л" * 80)
_LIVE_ASSISTANT = "LIVE_ASSISTANT_FULL_" + ("б" * 80)


def test_retention_window_start_index():
    assert retention_window_start_index(0) == 0
    assert retention_window_start_index(1) == 0
    assert retention_window_start_index(2) == 0
    assert retention_window_start_index(3) == 1
    assert retention_window_start_index(5) == 3
    assert retention_window_start_index(4, keep=1) == 3
    assert retention_window_start_index(3, keep=0) == 3


def test_payload_mode_lights_only_tools_outside_window():
    for kind in ("user", "agent", "summary", "ask_human", "todo", "error_msg"):
        assert payload_mode(kind, False) == "full"
        assert payload_mode(kind, True) == "full"
    for kind in ("call", "result", "error"):
        assert payload_mode(kind, False) == "light"
        assert payload_mode(kind, True) == "full"


def test_message_ids_in_retention_window_sees_preserved_copy():
    call = _call("read_file", "kept", {"path": "a.py"})
    out = _output("kept", "body")
    segments = [
        _seg([call], compacted=True, index=0),
        _seg(
            [_call("read_file", "kept", {"path": "a.py"}), out],
            compacted=True,
            index=1,
        ),
        _seg([_user("live", "u-live")], index=2),
    ]
    ids = message_ids_in_retention_window(segments)
    assert ids == {"id_kept", "out_kept", "u-live"}
    only_old = [
        _seg(
            [_call("read_file", "old", {"path": "o"})],
            compacted=True,
            index=0,
        ),
        _seg([_user("m", "u-mid")], compacted=True, index=1),
        _seg([_user("l", "u-live2")], index=2),
    ]
    assert "id_old" not in message_ids_in_retention_window(only_old)


def test_subagent_parent_ids_only_last_two_segments():
    segments = [
        _seg([_call("subagent_worker", "call_old", {"task": "old"})]),
        _seg([_call("read_file", "call_mid", {"path": "m"})]),
        _seg([_call("subagent_worker", "call_live", {"task": "now"})]),
    ]
    assert subagent_parent_ids_in_window(segments) == {"call_live"}


def test_filter_sidecar_drops_parents_outside_window():
    records = [
        {
            "parent_call_id": "call_old",
            "kind": "call",
            "payload": {"output": "x"},
        },
        {
            "parent_call_id": "call_live",
            "kind": "call",
            "payload": {"output": "y"},
        },
        {"kind": "call"},
    ]
    kept = filter_sidecar_to_parents(records, {"call_live"})
    assert [r["parent_call_id"] for r in kept] == ["call_live"]


def test_parent_outside_retention_only_when_call_is_light():
    from gc_tui.models import TraceEvent
    from gc_tui.services.trace_retention import lighten_tool_payload

    full = TraceEvent(
        no=1,
        event_ts_utc_iso="",
        kind="call",
        title="subagent_worker",
        summary="",
        full_payload={
            "type": "function_call",
            "call_id": "p",
            "arguments": "{}",
        },
        call_id="p",
        tool_name="subagent_worker",
    )
    light = TraceEvent(
        no=2,
        event_ts_utc_iso="",
        kind="call",
        title="subagent_worker",
        summary="",
        full_payload=lighten_tool_payload(
            {
                "type": "function_call",
                "name": "subagent_worker",
                "call_id": "q",
            }
        ),
        call_id="q",
        tool_name="subagent_worker",
    )
    assert parent_outside_retention([full, light], "q") is True
    assert parent_outside_retention([full, light], "p") is False
    assert parent_outside_retention([full], "missing") is False


def _call(name: str, call_id: str, args: dict) -> dict:
    return {
        "payload": {
            "id": f"id_{call_id}",
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
    }


def _output(call_id: str, output: str) -> dict:
    return {
        "payload": {
            "id": f"out_{call_id}",
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        }
    }


def _user(text: str, mid: str) -> dict:
    return {"payload": {"id": mid, "role": "user", "content": text}}


def _assistant(text: str, mid: str) -> dict:
    return {
        "payload": {
            "id": mid,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": text, "annotations": []}
            ],
        }
    }


def _finish(call_id: str, report: str) -> dict:
    return _call(
        "submit_solution",
        call_id,
        {"solution_type": "answer_question", "task_report": report},
    )


def _seg(messages: list, *, compacted: bool = False, index: int = 0) -> dict:
    return {
        "index": index,
        "messages": messages,
        "compacted_by": (
            {
                "ts": "",
                "messages_in_segment": 10,
                "synthesis": f"S{index}",
            }
            if compacted
            else None
        ),
    }


def _three_segments():
    old = _seg(
        [
            _user(_OLD_USER, "u0"),
            _assistant(_OLD_ASSISTANT, "a0"),
            _finish("call_finish_old", _OLD_FINISH),
            _call("read_file", "call_old", {"path": "old.py", "blob": _HEAVY}),
            _output("call_old", _HEAVY),
            _call(
                "subagent_worker",
                "call_sub_old",
                {"task": "старая задача " + _HEAVY, "reason": "потому что"},
            ),
            _output(
                "call_sub_old", '{"ok": true, "result": "' + _HEAVY + '"}'
            ),
        ],
        compacted=True,
        index=0,
    )
    mid = _seg(
        [
            _user(_MID_USER, "u1"),
            _call("read_file", "call_mid", {"path": "mid.py", "blob": _HEAVY}),
            _output("call_mid", _HEAVY),
        ],
        compacted=True,
        index=1,
    )
    live = _seg(
        [
            _user(_LIVE_USER, "u2"),
            _assistant(_LIVE_ASSISTANT, "a2"),
            _call(
                "read_file", "call_live", {"path": "live.py", "blob": _HEAVY}
            ),
            _output("call_live", _HEAVY),
            _call(
                "subagent_worker",
                "call_sub_live",
                {"task": "живая задача", "reason": "сейчас"},
            ),
            _output("call_sub_live", '{"ok": true}'),
        ],
        index=2,
    )
    return [old, mid, live]


def _sidecar():
    def pair(parent: str, child: str, body: str) -> list[dict]:
        return [
            {
                "parent_call_id": parent,
                "kind": "call",
                "seq": 1,
                "payload": {
                    "type": "function_call",
                    "name": "fs_read_file",
                    "arguments": json.dumps({"path": body}),
                    "call_id": child,
                },
            },
            {
                "parent_call_id": parent,
                "kind": "result",
                "seq": 2,
                "payload": {
                    "type": "function_call_output",
                    "call_id": child,
                    "output": body,
                },
            },
            {
                "parent_call_id": parent,
                "kind": "intermediate_reply",
                "seq": 3,
                "payload": {"prose": body, "call_id": f"ir_{child}"},
            },
        ]

    return pair("call_sub_old", "child_old", "NESTED_OLD_" + _HEAVY) + pair(
        "call_sub_live", "child_live", "NESTED_LIVE"
    )


class _SidecarStore:
    def __init__(self, records):
        self.records = records

    def read_subagent_trace(self, task_id):
        return self.records


def _event(scr, call_id: str, kind: str):
    for ev in scr._task_events:
        if ev.call_id == call_id and ev.kind == kind:
            return ev
    raise AssertionError(f"нет {kind} {call_id}")


def _assert_prose_full(scr):
    users = [ev.summary for ev in scr._task_events if ev.kind == "user"]
    assert _OLD_USER in users
    assert _MID_USER in users
    assert _LIVE_USER in users
    agents = [ev.summary for ev in scr._task_events if ev.kind == "agent"]
    assert _OLD_ASSISTANT in agents
    assert _OLD_FINISH in agents
    assert _LIVE_ASSISTANT in agents
    finish = _event(scr, "call_finish_old", "agent")
    assert _OLD_FINISH in json.dumps(finish.full_payload, ensure_ascii=False)
    assert finish.full_payload.get("_retention") != "light"


def _assert_tool_light(ev):
    payload = ev.full_payload
    assert payload.get("_retention") == "light"
    assert "arguments" not in payload
    assert "output" not in payload
    blob = json.dumps(payload, ensure_ascii=False)
    assert _HEAVY not in blob


def _assert_tool_full(ev):
    blob = json.dumps(ev.full_payload, ensure_ascii=False)
    assert ev.full_payload.get("_retention") != "light"
    assert _HEAVY in blob


@pytest.mark.asyncio
async def test_render_segments_keeps_prose_and_lights_tools_outside_window(
    tmp_path,
):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from gc_tui.widgets.user_reply import UserReplyContainer

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._store = _SidecarStore(_sidecar())
        scr._render_segments(_three_segments())
        scr._merge_sidecar_nested("t")
        await pilot.pause()

        texts = [r._text for r in scr.query(UserReplyContainer)]
        assert texts == [_OLD_USER, _MID_USER, _LIVE_USER]
        _assert_prose_full(scr)

        _assert_tool_light(_event(scr, "call_old", "call"))
        _assert_tool_light(_event(scr, "call_old", "result"))
        _assert_tool_light(_event(scr, "call_sub_old", "call"))
        _assert_tool_full(_event(scr, "call_mid", "call"))
        _assert_tool_full(_event(scr, "call_mid", "result"))
        _assert_tool_full(_event(scr, "call_live", "call"))
        _assert_tool_full(_event(scr, "call_live", "result"))
        live_parent = _event(scr, "call_sub_live", "call")
        assert live_parent.full_payload.get("_retention") != "light"
        assert "arguments" in live_parent.full_payload

        rows = {r.call_id: r for r in scr.query(ToolCallRow) if not r._nested}
        assert rows["call_old"]._tool_name == "read_file"
        assert rows["call_sub_old"]._tool_name == "subagent_worker"
        assert "потому что" in rows["call_sub_old"]._args
        assert "call_live" in rows

        nested = {r.call_id for r in scr.query(ToolCallRow) if r._nested}
        assert nested == {"child_live"}
        assert "child_old" not in scr._nested_tool_rows
        prose = [
            getattr(w, "_content", "")
            for w in scr._nested_widgets_by_parent.get("call_sub_live", [])
        ]
        assert "NESTED_LIVE" in prose
        assert "call_sub_old" not in scr._nested_widgets_by_parent


@pytest.mark.asyncio
async def test_tool_detail_hint_outside_window_opens_inside(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.messages import OpenToolCallDetailRequest
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_segments(_three_segments())
        await pilot.pause()

        scr.on_open_tool_call_detail_request(
            OpenToolCallDetailRequest("call_old")
        )
        await pilot.pause()
        assert not isinstance(pilot.app.screen, ToolCallDetailScreen)
        footer = str(pilot.app.query_one("#footer-left").render())
        assert RETENTION_DETAIL_HINT in footer

        scr.on_open_tool_call_detail_request(
            OpenToolCallDetailRequest("call_live")
        )
        await pilot.pause()
        assert isinstance(pilot.app.screen, ToolCallDetailScreen)
        body = "\n".join(
            str(s.render()) for s in pilot.app.screen.query("Static")
        )
        assert _HEAVY[:40] in body


@pytest.mark.asyncio
async def test_stream_reset_rebuild_drops_former_window_tools(tmp_path):
    """Бывшее окно из 2 сегментов после новой summary становится light."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.tool_call_row import ToolCallRow

    two = _three_segments()[:2]
    two[1]["compacted_by"] = None
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._store = _SidecarStore(_sidecar())
        scr._render_segments(two)
        scr._merge_sidecar_nested("t")
        await pilot.pause()
        _assert_tool_full(_event(scr, "call_old", "call"))
        assert "child_old" in {
            r.call_id for r in scr.query(ToolCallRow) if r._nested
        }

        scr._clear_trace()
        scr._reset_render_state()
        scr._render_segments(_three_segments())
        scr._merge_sidecar_nested("t")
        await pilot.pause()

        _assert_prose_full(scr)
        _assert_tool_light(_event(scr, "call_old", "call"))
        _assert_tool_light(_event(scr, "call_old", "result"))
        assert "child_old" not in scr._nested_tool_rows
        assert "child_live" in {
            r.call_id for r in scr.query(ToolCallRow) if r._nested
        }
        assert any(r.call_id == "call_sub_old" for r in scr.query(ToolCallRow))


@pytest.mark.asyncio
async def test_preview_same_retention_and_drops_sidecar_list(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.preview import PreviewScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from gc_tui.widgets.user_reply import UserReplyContainer

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(
            PreviewScreen(
                segments=_three_segments(),
                thread_id="preview1",
                subagent_records=_sidecar(),
            )
        )
        await pilot.pause()
        scr = pilot.app.screen
        assert isinstance(scr, PreviewScreen)
        assert scr._segments == []
        assert scr._subagent_records == []
        texts = [r._text for r in scr.query(UserReplyContainer)]
        assert _OLD_USER in texts and _LIVE_USER in texts
        nested = {r.call_id for r in scr.query(ToolCallRow) if r._nested}
        assert nested == {"child_live"}
        parents = {r.call_id for r in scr.query(ToolCallRow) if not r._nested}
        assert "call_sub_old" in parents
        assert "call_old" in parents


def _preserved_subagent_segments(*, copy_in_window: bool):
    """Тот же id вызова в старом сегменте и в копии.

    copy_in_window: копия в segments[-2:] (3 сегмента).
    Иначе копия тоже вне окна (4 сегмента, окно — последние два).
    """
    call = _call(
        "subagent_worker",
        "call_sub_kept",
        {"task": "задача " + _HEAVY, "reason": "потому что"},
    )
    out = _output("call_sub_kept", '{"ok": true, "result": "' + _HEAVY + '"}')
    copy_call = _call(
        "subagent_worker",
        "call_sub_kept",
        {"task": "задача " + _HEAVY, "reason": "потому что"},
    )
    copy_out = _output(
        "call_sub_kept", '{"ok": true, "result": "' + _HEAVY + '"}'
    )
    old = _seg([call, out], compacted=True, index=0)
    if copy_in_window:
        mid = _seg(
            [copy_call, copy_out, _user(_MID_USER, "u-mid")],
            compacted=True,
            index=1,
        )
        live = _seg([_user(_LIVE_USER, "u-live")], index=2)
        return [old, mid, live]
    aged = _seg([copy_call, copy_out], compacted=True, index=1)
    window = _seg(
        [_user(_MID_USER, "u-mid")],
        compacted=True,
        index=2,
    )
    live = _seg([_user(_LIVE_USER, "u-live")], index=3)
    return [old, aged, window, live]


def _sidecar_kept():
    return [
        {
            "parent_call_id": "call_sub_kept",
            "kind": "call",
            "seq": 1,
            "payload": {
                "type": "function_call",
                "name": "fs_read_file",
                "arguments": json.dumps({"path": "kept.py"}),
                "call_id": "child_kept",
            },
        },
        {
            "parent_call_id": "call_sub_kept",
            "kind": "intermediate_reply",
            "seq": 2,
            "payload": {"prose": "NESTED_KEPT", "call_id": "ir_child_kept"},
        },
    ]


@pytest.mark.asyncio
async def test_preserved_id_in_window_keeps_first_row_full(tmp_path):
    """Первая строка одна, но full, пока копия с тем же id в окне."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.messages import OpenToolCallDetailRequest
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._store = _SidecarStore(_sidecar_kept())
        scr._render_segments(_preserved_subagent_segments(copy_in_window=True))
        scr._merge_sidecar_nested("t")
        await pilot.pause()

        rows = [
            r for r in scr.query(ToolCallRow) if r.call_id == "call_sub_kept"
        ]
        assert len(rows) == 1
        assert not rows[0]._nested
        kept = _event(scr, "call_sub_kept", "call")
        _assert_tool_full(kept)
        _assert_tool_full(_event(scr, "call_sub_kept", "result"))
        nested = {r.call_id for r in scr.query(ToolCallRow) if r._nested}
        assert nested == {"child_kept"}

        scr.on_open_tool_call_detail_request(
            OpenToolCallDetailRequest("call_sub_kept")
        )
        await pilot.pause()
        assert isinstance(pilot.app.screen, ToolCallDetailScreen)


@pytest.mark.asyncio
async def test_preserved_id_outside_window_stays_light(tmp_path):
    """Обе копии вне segments[-2:] — строка light, nested нет."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.tool_call_row import ToolCallRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._store = _SidecarStore(_sidecar_kept())
        scr._render_segments(
            _preserved_subagent_segments(copy_in_window=False)
        )
        scr._merge_sidecar_nested("t")
        await pilot.pause()

        rows = [
            r for r in scr.query(ToolCallRow) if r.call_id == "call_sub_kept"
        ]
        assert len(rows) == 1
        _assert_tool_light(_event(scr, "call_sub_kept", "call"))
        _assert_tool_light(_event(scr, "call_sub_kept", "result"))
        assert "child_kept" not in {
            r.call_id for r in scr.query(ToolCallRow) if r._nested
        }


class _OneRecordLookup:
    """Журнал правок из одной записи — то, что читает инлайн-дифф ленты."""

    def __init__(self, record):
        self._record = record

    def by_call_id(self, call_id):
        return self._record if call_id == self._record.call_id else None

    def turn_ids(self):
        return [self._record.turn_id]


@pytest.mark.asyncio
async def test_edit_block_survives_retention_outside_window(tmp_path):
    """Правка в сегменте вне окна: payload облегчён, блок правки — на месте.

    Блок ищет запись журнала по `call_id`, а retention оставляет в
    облегчённом событии именно идентичность вызова. Потеряй лёгкий payload
    `call_id` — старые правки молча пропали бы из ленты."""
    pytest.importorskip("textual")
    from gc_core.agent_edits.journal import EditRecord
    from gc_tui.app import TuiApp
    from gc_tui.widgets.diff_block import DiffBlock

    record = EditRecord(
        path="old.py",
        turn_id="t0",
        ts=1.0,
        call_id="call_edit_old",
        op="edit",
        hunks="--- \n+++ \n@@ -1 +1 @@\n-a\n+b\n",
        add=1,
        del_=1,
        before_sha256=None,
        after_sha256=None,
        flags=[],
    )
    segments = _three_segments()
    segments[0]["messages"] += [
        _call(
            "fs_write_file",
            "call_edit_old",
            {"path": "old.py", "content": _HEAVY},
        ),
        _output("call_edit_old", '{"ok": true}'),
    ]
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._store = _SidecarStore(_sidecar())
        scr._edits_lookup_for_thread = lambda: _OneRecordLookup(record)
        scr._render_segments(segments)
        await pilot.pause()

        _assert_tool_light(_event(scr, "call_edit_old", "call"))
        blocks = [b for b in scr.query(DiffBlock) if b._record is record]
        assert len(blocks) == 1, "блок правки вне окна retention пропал"


class _Ev:
    def __init__(self, message_id):
        self.message_id = message_id


def _evs(*ids):
    return [_Ev(i) for i in ids]


def test_events_leaving_window_picks_third_from_end():
    evs = _evs("a", "b", "c", "d", "e", "f")
    out = events_leaving_window(evs, [0, 3, 5], {"b"})
    assert out == [evs[0], evs[2]]


def test_events_leaving_window_none_when_two_segments():
    assert events_leaving_window(_evs("a", "b", "c"), [0, 2], set()) == []


def test_events_leaving_window_four_segments_takes_all_outside():
    evs = _evs("a", "b", "c", "d", "e")
    assert events_leaving_window(evs, [0, 1, 3, 4], {"a"}) == evs[1:3]
    assert events_leaving_window(evs, [0, 1, 3, 4], set()) == evs[:3]
