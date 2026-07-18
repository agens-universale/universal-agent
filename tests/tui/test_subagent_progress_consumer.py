"""Tests for subagent progress TUI consumer (drain → nested rows / heartbeat)."""
from __future__ import annotations

from tools.subagents.progress import QueueSubagentProgressSink, SubagentProgressEvent
from tui.services.subagent_progress import (
    SubagentProgressConsumer,
    plan_progress_actions,
)


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


class TestPlanProgressActions:
    def test_tool_start_then_end(self):
        actions = plan_progress_actions(
            [
                _ev(kind="tool_start", tool_name="fs_read_file", tool_call_id="tc1",
                    args_summary="path='a.py'"),
                _ev(kind="tool_end", tool_name="fs_read_file", tool_call_id="tc1",
                    ok=True, elapsed_ms=12),
            ]
        )
        kinds = [a.kind for a in actions]
        assert kinds == [
            "mount_nested_tool",
            "parent_heartbeat",
            "finish_nested_tool",
            "parent_heartbeat",
        ]
        assert actions[0].tool_name == "fs_read_file"
        assert actions[0].tool_call_id == "tc1"
        assert actions[2].ok is True

    def test_intermediate_reply(self):
        actions = plan_progress_actions(
            [_ev(kind="intermediate_reply", prose="Сейчас проверю", tool_name="turn_intermediate_reply")]
        )
        assert actions[0].kind == "mount_nested_prose"
        assert actions[0].prose == "Сейчас проверю"
        assert any(a.kind == "parent_heartbeat" for a in actions)

    def test_user_message(self):
        actions = plan_progress_actions(
            [_ev(kind="user_message", prose="смотри auth")]
        )
        assert actions[0].kind == "mount_nested_user"
        assert actions[0].prose == "смотри auth"
        assert any(a.kind == "parent_heartbeat" for a in actions)

    def test_many_tools_all_mounted_no_overflow(self):
        events = [
            _ev(
                kind="tool_start",
                tool_name=f"t{i}",
                tool_call_id=f"tc{i}",
                step=i,
            )
            for i in range(40)
        ]
        actions = plan_progress_actions(events)
        mounts = [a for a in actions if a.kind == "mount_nested_tool"]
        assert len(mounts) == 40
        assert not any(a.kind == "nested_overflow" for a in actions)

    def test_finished_keeps_nested_no_collapse(self):
        actions = plan_progress_actions(
            [
                _ev(kind="tool_start", tool_name="fs_read_file", tool_call_id="tc1"),
                _ev(kind="finished", step=5),
            ]
        )
        kinds = [a.kind for a in actions]
        assert "collapse_nested" not in kinds
        assert kinds[-1] == "parent_heartbeat"
        assert any(a.kind == "mount_nested_tool" for a in actions)


class TestConsumerApply:
    def test_apply_mounts_and_finishes_via_hooks(self):
        sink = QueueSubagentProgressSink()
        sink.emit(_ev(kind="tool_start", tool_name="fs_read_file",
                      tool_call_id="tc1", args_summary="p"))
        sink.emit(_ev(kind="tool_end", tool_name="fs_read_file",
                      tool_call_id="tc1", ok=True))

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
