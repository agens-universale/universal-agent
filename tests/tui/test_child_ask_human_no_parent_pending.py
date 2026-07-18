"""Child turn_ask_human answer must not land in parent pending / data_*."""

from __future__ import annotations

import uuid

import pytest


class _FakeRunner:
    def __init__(self):
        self.submitted: list[str] = []

    def is_running(self) -> bool:
        return True

    def submit_ask_human_reply(self, text: str) -> None:
        self.submitted.append(text)


@pytest.mark.asyncio
async def test_child_ask_human_answer_no_parent_pending(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.stores.file_store import FileTaskStore
    from tui.widgets.command_input import CommandTextArea
    from tui.widgets.tool_call_row import ToolCallRow
    from tui.widgets.user_reply import UserReplyContainer

    threads = tmp_path / "threads"
    threads.mkdir()
    app = TuiApp(threads_dir=str(threads), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        tid = uuid.uuid4().hex
        scr._active_task_id = tid
        app.tui_state.active_task_id = tid
        scr._store = FileTaskStore(threads_dir=str(threads), system="cli")
        # meta stub so _track_pending could write if called
        scr._store.update_tui_meta(tid, {"status": "running"})

        runner = _FakeRunner()
        app.active_runner = runner

        parent = ToolCallRow(
            call_id="call_sub_1",
            tool_name="subagent_worker",
            args_summary="task",
        )
        scr._active_calls["call_sub_1"] = parent
        scr._subagent_parent_rows["call_sub_1"] = parent
        from textual.containers import VerticalScroll

        scr.query_one("#trace", VerticalScroll).mount(parent)

        scr._show_child_ask_human(
            parent_call_id="call_sub_1",
            prose="Что дальше?",
            tool_call_id="call_ah_1",
        )
        await pilot.pause()
        assert scr._ask_human_banner is not None
        assert scr._ask_human_child_parent_call_id == "call_sub_1"

        submitted = CommandTextArea.Submitted(value="копни глубже")
        scr.on_command_text_area_submitted(submitted)
        await pilot.pause()

        assert runner.submitted == ["копни глубже"]
        assert scr._ask_human_banner is None
        assert scr._ask_human_child_parent_call_id is None

        # Parent pending path must NOT run.
        assert not list(scr.query(UserReplyContainer))
        meta = scr._store.get_task_meta(tid)
        pending = (meta.get("tui") or {}).get("pending_user_messages") or []
        assert pending == []

        # Nested user row under subagent is ok (display only).
        from tui.widgets.trace_row import TraceRow

        nested_user = [
            r
            for r in scr.query(TraceRow)
            if getattr(r, "_content", "") == "копни глубже"
            and "line-user" in r.classes
        ]
        assert nested_user, "ответ child HITL должен быть nested user-строкой"


@pytest.mark.asyncio
async def test_parent_ask_human_answer_still_tracks_pending(tmp_path):
    """Регрессия: parent ask_human по-прежнему монтирует pending user-reply."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.models import TraceEvent
    from tui.services.stores.file_store import FileTaskStore
    from tui.widgets.command_input import CommandTextArea
    from tui.widgets.user_reply import UserReplyContainer

    threads = tmp_path / "threads"
    threads.mkdir()
    app = TuiApp(threads_dir=str(threads), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        tid = uuid.uuid4().hex
        scr._active_task_id = tid
        app.tui_state.active_task_id = tid
        scr._store = FileTaskStore(threads_dir=str(threads), system="cli")
        scr._store.update_tui_meta(tid, {"status": "running"})

        runner = _FakeRunner()
        app.active_runner = runner
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="2026-07-18T10:00:00+00:00",
            kind="ask_human",
            title="ask_human",
            summary="Готовы?",
            full_payload={},
            call_id="call_live_1",
            tool_name="ask_human",
        )
        scr._on_ask_human_event(ev, live=True)
        await pilot.pause()
        assert scr._ask_human_child_parent_call_id is None

        scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value="да")
        )
        await pilot.pause()
        assert runner.submitted == ["да"]
        replies = list(scr.query(UserReplyContainer))
        assert replies and replies[-1]._state == "pending"
