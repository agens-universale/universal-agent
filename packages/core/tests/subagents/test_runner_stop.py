"""Tests for stop_requested propagation into subagent."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from gc_core.states import SharedStateData, State
from gc_core.tools.subagents.runner import SubagentRunner


def _make_orchestrator_state():
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "fs_read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="System\n<specific_role>x</specific_role>",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    return state


def _make_config(**overrides):
    cfg = {
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "enabled_tools": [],
        "skills_data_path": "",
        "mcp_servers": [],
        "model": "test-model",
        "system_prompt_id": "test-prompt",
        "subagent_max_steps": 30,
    }
    cfg.update(overrides)
    return cfg


def _make_ctx(cfg=None):
    return SimpleNamespace(
        config_dict=cfg or _make_config(),
        auth_connector=None,
        logger=None,
        logging_path="",
        summary_trace_path="",
        tools_data={},
        tools_spec=[],
        tools_description=[],
    )


class TestStopRequestedPropagation:
    async def test_check_stop_passed_to_run_agent(self):
        """run_agent_func receives check_stop kwarg."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        captured = {}

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured["kwargs"] = kwargs
            return "done"

        runner = SubagentRunner(ctx, orch, cfg)

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            await runner.arun(
                "subagent_worker", {"task": "test"}, fake_run_agent
            )

        assert "check_stop" in captured["kwargs"]
        assert callable(captured["kwargs"]["check_stop"])

    async def test_check_stop_reflects_orchestrator_stop(self):
        """check_stop returns True when orchestrator state.stop_requested is set."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        captured = {}

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured["check_stop"] = kwargs["check_stop"]
            return "done"

        runner = SubagentRunner(ctx, orch, cfg)

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            await runner.arun(
                "subagent_worker", {"task": "test"}, fake_run_agent
            )

        check_stop = captured["check_stop"]

        assert check_stop() is False
        orch.stop_requested = True
        assert check_stop() is True

    async def test_stopped_subagent_returns_stub_message(self):
        """When run_agent returns None (stopped), run() returns a stub message."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            check_stop = kwargs.get("check_stop")
            orch.stop_requested = True
            if check_stop and check_stop():
                return None
            return "should not reach"

        runner = SubagentRunner(ctx, orch, cfg)

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            result = await runner.arun(
                "subagent_worker", {"task": "test"}, fake_run_agent
            )

        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["status"] == "cancelled_by_user"
        assert payload["error"]["code"] == "cancelled_by_user"
        assert payload["meta"]["finish_reason"] == "stopped_by_user"

    async def test_stopped_subagent_includes_child_run_id_in_meta(self):
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            return None

        runner = SubagentRunner(ctx, orch, cfg)
        with patch.object(
            runner,
            "_build_child_ctx",
            return_value=SimpleNamespace(run_id="child-run-42"),
        ):
            result = await runner.arun(
                "subagent_worker", {"task": "test"}, fake_run_agent
            )

        payload = json.loads(result)
        assert payload["meta"]["child_run_id"] == "child-run-42"
        assert payload["meta"]["finish_reason"] == "stopped_by_user"

    async def test_normal_run_check_stop_returns_false(self):
        """check_stop returns False when orchestrator is not stopped."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        stop_values = []

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            stop_values.append(kwargs["check_stop"]())
            return "ok"

        runner = SubagentRunner(ctx, orch, cfg)

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            await runner.arun(
                "subagent_worker", {"task": "test"}, fake_run_agent
            )

        assert stop_values == [False]
