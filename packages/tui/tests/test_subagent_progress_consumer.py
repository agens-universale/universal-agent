"""Tests for subagent progress TUI consumer (drain → nested rows / heartbeat)."""

from __future__ import annotations

from gc_core.execution.progress import (
    QueueSubagentProgressSink,
    SubagentProgressEvent,
)
from gc_tui.services.subagent_progress import SubagentProgressConsumer


def _ev(**kwargs) -> SubagentProgressEvent:
    base = dict(
        parent_call_id="parent_1",
        child_run_id="child_1",
        kind="tool_start",
        step=1,
        max_steps=50,
        elapsed_ms=0,
    )
    base.update(kwargs)
    return SubagentProgressEvent(**base)


class TestConsumerApply:
    def test_apply_mounts_and_finishes_via_hooks(self):
        sink = QueueSubagentProgressSink()
        sink.emit(
            _ev(
                kind="tool_start",
                tool_name="fs_read_file",
                tool_call_id="tc1",
                args_summary="p",
            )
        )
        sink.emit(
            _ev(
                kind="tool_end",
                tool_name="fs_read_file",
                tool_call_id="tc1",
                ok=True,
            )
        )

        mounted: list[dict] = []
        finished: list[dict] = []
        heartbeats: list[dict] = []

        consumer = SubagentProgressConsumer()
        consumer.apply_drained(
            sink.drain(),
            mount_nested_tool=lambda **kw: mounted.append(kw),
            finish_nested_tool=lambda **kw: finished.append(kw),
            mount_nested_prose=lambda **kw: None,
            parent_heartbeat=lambda **kw: heartbeats.append(kw),
        )
        assert len(mounted) == 1
        assert mounted[0]["tool_call_id"] == "tc1"
        assert len(finished) == 1
        assert finished[0]["ok"] is True
        assert heartbeats  # at least one after end

    def test_user_message_images_reach_the_nested_row(self):
        """Реплика ребёнку с картинкой — строка со строкой картинки, как у ответа
        на его вопрос (смоук 2026-10-01)."""
        from image_helpers import make_attachment

        att = make_attachment("a" * 16, 1)
        mounted: list[dict] = []
        SubagentProgressConsumer().apply_drained(
            [
                _ev(
                    kind="user_message",
                    prose="[Изображение #1] что тут?",
                    attachments=[att.to_dict()],
                )
            ],
            mount_nested_tool=lambda **kw: None,
            finish_nested_tool=lambda **kw: None,
            mount_nested_prose=lambda **kw: None,
            parent_heartbeat=lambda **kw: None,
            mount_nested_user=lambda **kw: mounted.append(kw),
        )
        assert mounted[0]["images"] == (att,)

    def test_session_todo_tool_start_routes_to_todo_mount(self):
        consumer = SubagentProgressConsumer()
        mounted: list[tuple[str, dict]] = []
        ev = _ev(
            kind="tool_start",
            tool_name="session_todo",
            tool_call_id="c1",
            todo_items=[{"content": "a", "status": "pending"}],
        )
        consumer.apply_drained(
            [ev],
            mount_nested_tool=lambda **kw: mounted.append(("tool", kw)),
            finish_nested_tool=lambda **kw: None,
            mount_nested_prose=lambda **kw: None,
            parent_heartbeat=lambda **kw: None,
            mount_nested_todo=lambda **kw: mounted.append(("todo", kw)),
        )
        kinds = [k for k, _ in mounted]
        assert "todo" in kinds and "tool" not in kinds

    def test_session_todo_every_call_routes_to_todo_mount(self):
        """v3 спеки: разворачиваем каждый вызов, включая смену только статусов."""
        consumer = SubagentProgressConsumer()
        mounted: list[tuple[str, dict]] = []
        first = _ev(
            kind="tool_start",
            tool_name="session_todo",
            tool_call_id="c1",
            todo_items=[{"content": "a", "status": "pending"}],
        )
        second = _ev(
            kind="tool_start",
            tool_name="session_todo",
            tool_call_id="c2",
            todo_items=[{"content": "a", "status": "completed"}],
        )
        consumer.apply_drained(
            [first, second],
            mount_nested_tool=lambda **kw: mounted.append(("tool", kw)),
            finish_nested_tool=lambda **kw: None,
            mount_nested_prose=lambda **kw: None,
            parent_heartbeat=lambda **kw: None,
            mount_nested_todo=lambda **kw: mounted.append(("todo", kw)),
        )
        assert [k for k, _ in mounted] == ["todo", "todo"]

    def test_session_todo_without_todo_callback_falls_back_to_tool_row(self):
        consumer = SubagentProgressConsumer()
        mounted: list[tuple[str, dict]] = []
        ev = _ev(
            kind="tool_start",
            tool_name="session_todo",
            tool_call_id="c1",
            todo_items=[{"content": "a", "status": "pending"}],
        )
        consumer.apply_drained(
            [ev],
            mount_nested_tool=lambda **kw: mounted.append(("tool", kw)),
            finish_nested_tool=lambda **kw: None,
            mount_nested_prose=lambda **kw: None,
            parent_heartbeat=lambda **kw: None,
        )
        assert [k for k, _ in mounted] == ["tool"]


def _finish_of(**end) -> dict:
    """Аргументы `finish_nested_tool` для одного `tool_end` с ``end``."""
    finished: list[dict] = []
    SubagentProgressConsumer().apply_drained(
        [_ev(kind="tool_end", tool_call_id="tc1", ok=False, **end)],
        mount_nested_tool=lambda **kw: None,
        finish_nested_tool=lambda **kw: finished.append(kw),
        mount_nested_prose=lambda **kw: None,
        parent_heartbeat=lambda **kw: None,
    )
    (kw,) = finished
    return kw


class TestNestedCallOutcome:
    """Статус вложенного вызова вживую — фактами ядра на `tool_end`."""

    def test_step_limit_is_halted(self):
        kw = _finish_of(error_brief="limit", result_status="step_limit")
        assert (kw["outcome"], kw["error_brief"]) == ("halted", "лимит шагов")

    def test_user_stop_is_halted(self):
        kw = _finish_of(result_status="cancelled_by_user")
        assert (kw["outcome"], kw["error_brief"]) == ("halted", "отменено")

    def test_mode_denial_is_refused(self):
        kw = _finish_of(
            error_brief="x",
            result_error={
                "type": "write_denied_by_mode",
                "mode": "plan",
                "reason": "x",
            },
        )
        assert (kw["outcome"], kw["error_brief"]) == (
            "refused",
            "режим plan: запись запрещена",
        )

    def test_plain_error_stays_error(self):
        kw = _finish_of(
            error_brief="нет файла",
            result_error={"type": "not_found", "message": "нет файла"},
        )
        assert (kw["ok"], kw["outcome"], kw["error_brief"]) == (
            False,
            None,
            "нет файла",
        )

    def test_old_stream_without_facts_stays_error(self):
        kw = _finish_of(error_brief="boom")
        assert (kw["ok"], kw["outcome"], kw["error_brief"]) == (
            False,
            None,
            "boom",
        )


def _prose_of(**reply) -> dict:
    mounted: list[dict] = []
    SubagentProgressConsumer().apply_drained(
        [_ev(kind="intermediate_reply", prose="проверю", **reply)],
        mount_nested_tool=lambda **kw: None,
        finish_nested_tool=lambda **kw: None,
        mount_nested_prose=lambda **kw: mounted.append(kw),
        parent_heartbeat=lambda **kw: None,
    )
    (kw,) = mounted
    return kw


def test_rejected_intermediate_reply_carries_its_reason():
    kw = _prose_of(ok=False, error_brief="нет плана")
    assert (kw["ok"], kw["error_brief"]) == (False, "нет плана")


def test_accepted_intermediate_reply_is_ok():
    kw = _prose_of(ok=True)
    assert (kw["ok"], kw["error_brief"]) == (True, None)
