"""Step 1: SubagentProgressEvent + sink + wiring (call_id → parent_call_id)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from states import SharedStateData, State
from tools import apply_tool
from tools.subagents.progress import (
    NOOP_SUBAGENT_PROGRESS_SINK,
    QueueSubagentProgressSink,
    SubagentProgressEvent,
    resolve_progress_sink,
)
from tools.subagents.runner import SubagentRunner


def _make_event(**overrides) -> SubagentProgressEvent:
    base = dict(
        parent_call_id="call_parent_1",
        child_run_id="child_run_1",
        kind="tool_start",
        step=1,
        max_steps=50,
        elapsed_ms=12,
    )
    base.update(overrides)
    return SubagentProgressEvent(**base)


class TestSubagentProgressEvent:
    def test_required_fields(self):
        event = _make_event()
        assert event.parent_call_id == "call_parent_1"
        assert event.child_run_id == "child_run_1"
        assert event.kind == "tool_start"
        assert event.step == 1
        assert event.max_steps == 50
        assert event.elapsed_ms == 12

    def test_optional_fields_default_none(self):
        event = _make_event()
        assert event.tool_name is None
        assert event.tool_call_id is None
        assert event.args_summary is None
        assert event.prose is None
        assert event.ok is None
        assert event.error_brief is None
        assert event.usage_delta is None

    def test_optional_fields_set(self):
        event = _make_event(
            tool_name="fs_read_file",
            tool_call_id="tc_1",
            args_summary="path=…",
            prose="checking",
            ok=True,
            error_brief="boom",
            usage_delta={"prompt_tokens": 1},
        )
        assert event.tool_name == "fs_read_file"
        assert event.tool_call_id == "tc_1"
        assert event.args_summary == "path=…"
        assert event.prose == "checking"
        assert event.ok is True
        assert event.error_brief == "boom"
        assert event.usage_delta == {"prompt_tokens": 1}


class TestNoOpSink:
    def test_emit_is_safe(self):
        NOOP_SUBAGENT_PROGRESS_SINK.emit(_make_event())

    def test_resolve_none_returns_noop(self):
        ctx = SimpleNamespace()
        assert resolve_progress_sink(ctx) is NOOP_SUBAGENT_PROGRESS_SINK

    def test_resolve_explicit_none_attr_returns_noop(self):
        ctx = SimpleNamespace(subagent_progress_sink=None)
        assert resolve_progress_sink(ctx) is NOOP_SUBAGENT_PROGRESS_SINK

    def test_resolve_returns_set_sink(self):
        sink = QueueSubagentProgressSink()
        ctx = SimpleNamespace(subagent_progress_sink=sink)
        assert resolve_progress_sink(ctx) is sink


class TestQueueSink:
    def test_emit_and_drain(self):
        sink = QueueSubagentProgressSink()
        e1 = _make_event(kind="started", step=0)
        e2 = _make_event(kind="tool_start", step=1, tool_name="fs_read_file")
        sink.emit(e1)
        sink.emit(e2)
        drained = sink.drain()
        assert drained == [e1, e2]
        assert sink.drain() == []

    def test_thread_safe_drain_clears(self):
        sink = QueueSubagentProgressSink()
        sink.emit(_make_event(kind="heartbeat"))
        assert len(sink.drain()) == 1
        sink.emit(_make_event(kind="finished"))
        assert [e.kind for e in sink.drain()] == ["finished"]


def _make_state():
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "fs_read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="You are a helpful assistant.",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    return state


def _make_ctx(tools_data, *, sink=None):
    cfg = {
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "enabled_tools": [],
        "skills_data_path": "",
        "mcp_servers": [],
        "model": "test-model",
        "system_prompt_id": "test-prompt",
    }
    return SimpleNamespace(
        config_dict=cfg,
        tools_data=tools_data,
        auth_connector=None,
        logger=None,
        logging_path="",
        summary_trace_path="",
        tools_spec=[],
        tools_description=[],
        subagent_progress_sink=sink,
    )


class TestApplyToolPassesCallId:
    def test_call_id_forwarded_as_parent_call_id(self):
        from tools.subagents.registry import get_subagents_tools_data

        sub_data = get_subagents_tools_data()
        ctx = _make_ctx(sub_data)
        state = _make_state()
        run_agent_func = MagicMock(return_value="ok")

        with patch("tools.subagents.runner.SubagentRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.run.return_value = "ok"
            apply_tool(
                tool_name="subagent_worker",
                arguments=json.dumps({"task": "do", "reason": "r"}),
                ctx=ctx,
                state=state,
                run_agent_func=run_agent_func,
                call_id="fc_abc",
            )

        instance.run.assert_called_once_with(
            "subagent_worker",
            {"task": "do", "reason": "r"},
            run_agent_func,
            parent_call_id="fc_abc",
        )

    def test_missing_call_id_forwards_none(self):
        from tools.subagents.registry import get_subagents_tools_data

        sub_data = get_subagents_tools_data()
        ctx = _make_ctx(sub_data)
        state = _make_state()
        run_agent_func = MagicMock(return_value="ok")

        with patch("tools.subagents.runner.SubagentRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.run.return_value = "ok"
            apply_tool(
                tool_name="subagent_worker",
                arguments=json.dumps({"task": "do", "reason": "r"}),
                ctx=ctx,
                state=state,
                run_agent_func=run_agent_func,
            )

        kwargs = instance.run.call_args.kwargs
        assert kwargs.get("parent_call_id") is None


def _make_runner_config(tmp_path):
    return {
        "save_logs": True,
        "logs_dump_dir": str(tmp_path),
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "subagent_max_steps": 30,
        "enabled_tools": [],
        "skills_data_path": "",
        "mcp_servers": [],
        "model": "test-model",
        "system_prompt_id": "test-prompt",
        "chat_completions_params": {
            "llm": {"max_tokens": 8192, "completion_kwargs": {}},
        },
    }


class TestProcessToolCallsForwardsCallId:
    def test_apply_tool_receives_call_id(self, monkeypatch):
        from processing_responses import process_tool_calls

        state = _make_state()
        ctx = SimpleNamespace(config_dict={})
        seen: dict = {}

        def _fake_apply_tool(**kwargs):
            seen.update(kwargs)
            return '{"ok": true}'

        monkeypatch.setattr("processing_responses.apply_tool", _fake_apply_tool)
        process_tool_calls(
            messages=[
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_xyz",
                    "name": "fs_read_file",
                    "arguments": "{}",
                }
            ],
            state=state,
            ctx=ctx,
            run_agent_func=lambda *_a, **_k: None,
        )
        assert seen.get("call_id") == "call_xyz"


class TestRunnerWiresSinkOntoChildCtx:
    def test_child_ctx_gets_parent_call_id_and_sink(self, tmp_path):
        from tests.helpers.tool_ctx import local_ctx

        orch = _make_state()
        cfg = _make_runner_config(tmp_path)
        sink = QueueSubagentProgressSink()
        parent_ctx = local_ctx(cfg)
        parent_ctx.run_id = "parent_run_1"
        parent_ctx.auth_connector = None
        parent_ctx.logger = None
        parent_ctx.logging_path = str(tmp_path / "agent_main_parent.log")
        parent_ctx.summary_trace_path = str(tmp_path / "summary.jsonl")
        parent_ctx.tools_data = {}
        parent_ctx.tools_spec = []
        parent_ctx.tools_description = []
        parent_ctx.subagent_progress_sink = sink
        runner = SubagentRunner(parent_ctx, orch, cfg)
        captured: dict = {}

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured["child_ctx"] = child_ctx
            return "done"

        result = runner.run(
            "subagent_worker",
            {"task": "x", "reason": "y"},
            fake_run_agent,
            parent_call_id="call_42",
        )

        child_ctx = captured["child_ctx"]
        assert child_ctx.parent_call_id == "call_42"
        assert child_ctx.subagent_progress_sink is sink
        assert json.loads(result)["ok"] is True

    def test_child_ctx_sink_none_when_parent_has_no_sink(self, tmp_path):
        from tests.helpers.tool_ctx import local_ctx

        orch = _make_state()
        cfg = _make_runner_config(tmp_path)
        parent_ctx = local_ctx(cfg)
        parent_ctx.run_id = "parent_run_1"
        parent_ctx.auth_connector = None
        parent_ctx.logger = None
        parent_ctx.logging_path = str(tmp_path / "agent_main_parent.log")
        parent_ctx.summary_trace_path = str(tmp_path / "summary.jsonl")
        parent_ctx.tools_data = {}
        parent_ctx.tools_spec = []
        parent_ctx.tools_description = []
        runner = SubagentRunner(parent_ctx, orch, cfg)
        captured: dict = {}

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured["child_ctx"] = child_ctx
            return "done"

        runner.run(
            "subagent_worker",
            {"task": "x", "reason": "y"},
            fake_run_agent,
            parent_call_id="call_7",
        )

        child_ctx = captured["child_ctx"]
        assert child_ctx.parent_call_id == "call_7"
        assert getattr(child_ctx, "subagent_progress_sink", None) is None
        assert resolve_progress_sink(child_ctx) is NOOP_SUBAGENT_PROGRESS_SINK
