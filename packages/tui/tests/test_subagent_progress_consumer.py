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
