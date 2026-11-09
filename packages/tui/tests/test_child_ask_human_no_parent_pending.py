"""Child turn_ask_human answer must not land in parent pending / data_*."""

from __future__ import annotations
from gc_testkit.develop_files import meta_of

from gc_testkit.tui_support import tui_settings
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
    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.widgets.command_input import CommandTextArea
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from gc_tui.widgets.user_reply import UserReplyContainer

    threads = tmp_path / "threads"
    threads.mkdir()
    app = TuiApp(
        threads_dir=str(threads),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
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
            label="subagent_worker",
            target="task",
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
        await scr.on_command_text_area_submitted(submitted)
        await pilot.pause()

        assert runner.submitted == ["копни глубже"]
        assert scr._ask_human_banner is None
        assert scr._ask_human_child_parent_call_id is None

        # Parent pending path must NOT run.
        assert not list(scr.query(UserReplyContainer))
        meta = meta_of(scr._store.get_record(tid))
        pending = (meta.get("tui") or {}).get("pending_user_messages") or []
        assert pending == []

        # Nested user row under subagent is ok (display only).
        from gc_tui.widgets.trace_row import TraceRow

        nested_user = [
            r
            for r in scr.query(TraceRow)
            if str(getattr(r, "_content", "")) == "копни глубже"
            and "line-user" in r.classes
        ]
        assert nested_user, "ответ child HITL должен быть nested user-строкой"


@pytest.mark.asyncio
async def test_parent_ask_human_answer_still_tracks_pending(tmp_path):
    """Регрессия: parent ask_human по-прежнему монтирует pending user-reply."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.widgets.command_input import CommandTextArea
    from gc_tui.widgets.user_reply import UserReplyContainer

    threads = tmp_path / "threads"
    threads.mkdir()
    app = TuiApp(
        threads_dir=str(threads),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
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

        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value="да")
        )
        await pilot.pause()
        assert runner.submitted == ["да"]
        replies = list(scr.query(UserReplyContainer))
        assert replies and replies[-1]._state == "pending"


@pytest.mark.asyncio
async def test_child_ask_human_answer_with_images_renders_like_a_reply(
    tmp_path,
):
    """Ответ с картинками на вопрос субагента: HumanAnswer уходит ребёнку, а вложенная
    строка ответа рисует плашки и подстроки картинок, как реплика (контракт §3), и
    Ctrl+клик по ним открывает файл."""
    pytest.importorskip("textual")
    from rich.style import Style
    from textual import events
    from textual.containers import VerticalScroll

    from gc_core.attachments.model import chip_text
    from gc_core.tools.turn.ask_human import HumanAnswer
    from gc_tui.app import TuiApp
    from gc_tui.messages import OpenImageRequest
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.widgets.command_input import CommandTextArea
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from gc_tui.widgets.user_reply import NestedUserReply
    from image_helpers import make_attachment_file

    threads = tmp_path / "threads"
    threads.mkdir()
    app = TuiApp(
        threads_dir=str(threads),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    att = make_attachment_file(3, tmp_path)
    text = f"{chip_text(3)} вот ошибка"
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        tid = uuid.uuid4().hex
        scr._active_task_id = tid
        app.tui_state.active_task_id = tid
        scr._store = FileTaskStore(threads_dir=str(threads), system="cli")
        runner = _FakeRunner()
        app.active_runner = runner
        parent = ToolCallRow(
            call_id="call_sub_1",
            label="subagent_worker",
            target="task",
        )
        scr._active_calls["call_sub_1"] = parent
        scr._subagent_parent_rows["call_sub_1"] = parent
        scr.query_one("#trace", VerticalScroll).mount(parent)
        scr._show_child_ask_human(
            parent_call_id="call_sub_1",
            prose="Покажите ошибку?",
            tool_call_id="call_ah_1",
        )
        await pilot.pause()

        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value=text, attachments=(att,))
        )
        await pilot.pause()

        assert runner.submitted == [HumanAnswer(text=text, attachments=(att,))]
        (row,) = scr.query(NestedUserReply)
        content = row._content
        assert content.plain == f"{text}\n└ {chip_text(3)} · скрин.png"
        chip = content.plain.index(chip_text(3))
        assert any(
            span.start == chip
            and span.style.meta.get("image_id") == att.id
            and span.style.color is None
            for span in content.spans
        )  # цвет текста до наведения

        posted: list = []
        row.post_message = posted.append
        row.on_click(
            events.Click(
                widget=row,
                x=1,
                y=0,
                delta_x=0,
                delta_y=0,
                button=1,
                shift=False,
                meta=False,
                ctrl=True,
                style=Style(meta={"image_id": att.id}),
                chain=1,
            )
        )
        del row.post_message
        assert [m.path for m in posted if isinstance(m, OpenImageRequest)] == [
            att.path
        ]
