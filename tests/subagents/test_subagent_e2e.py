"""Tests for SubagentRunner.run — full subagent lifecycle."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

from persistence.state_storage import save_state, state_storage
from states import State, SharedStateData
from tools.subagents import runner as runner_module
from tools.subagents.runner import SubagentRunner


def _make_orchestrator_state(system_prompt="System\n<specific_role>x</specific_role>"):
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt=system_prompt,
        shared=SharedStateData(working_memory={"state": "initial"}),
    )
    state.backend_api_mode = "chat_completions"
    save_state(state)
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


def _make_ctx(config_dict=None):
    return SimpleNamespace(
        config_dict=config_dict or _make_config(),
        auth_connector=None,
        logger=None,
        logging_path="",
        summary_trace_path="",
        tools_data={},
        tools_spec=[],
        tools_description=[],
    )


class TestSubagentRunnerRun:
    def test_run_calls_run_agent_func(self):
        """run() should call run_agent_func with (messages, child_state, child_ctx)."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = MagicMock(return_value="done")

        runner = SubagentRunner(ctx, orch, cfg)

        with patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()):
            result = runner.run("subagent_worker", {"task": "do X", "reason": "test"}, run_agent_func)

        run_agent_func.assert_called_once()
        args = run_agent_func.call_args
        messages_arg = args[0][0]
        child_state_arg = args[0][1]

        assert isinstance(messages_arg, list)
        assert len(messages_arg) >= 1
        assert any(m["role"] == "user" for m in messages_arg)
        assert isinstance(child_state_arg, State)
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["result"] == "done"
        assert payload["meta"]["tool_name"] == "subagent_worker"

    def test_run_returns_string_result(self):
        """run() returns whatever run_agent_func returns (as string)."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = MagicMock(return_value="Task completed successfully")

        runner = SubagentRunner(ctx, orch, cfg)

        with patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()):
            result = runner.run("subagent_worker", {"task": "test"}, run_agent_func)

        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["result"] == "Task completed successfully"

    def test_run_returns_fallback_on_none(self):
        """run() returns fallback message if run_agent_func returns None."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = MagicMock(return_value=None)

        runner = SubagentRunner(ctx, orch, cfg)

        with patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()):
            result = runner.run("subagent_worker", {"task": "test"}, run_agent_func)

        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["meta"]["finish_reason"] == "error"
        assert payload["error"]["code"] == "subagent_stopped_by_user"
        assert "stopped" in payload["error"]["message"].lower()

    def test_shared_mutations_visible_to_orchestrator(self):
        """Subagent mutations on shared.working_memory are visible in orchestrator state."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            child_state.shared.working_memory["state"] = "updated by subagent"
            child_state.shared.working_memory["knowledge"] = "created"
            return "Subagent done"

        runner = SubagentRunner(ctx, orch, cfg)

        with patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()):
            runner.run("subagent_worker", {"task": "modify memory"}, fake_run_agent)

        assert orch.shared.working_memory["state"] == "updated by subagent"
        assert orch.shared.working_memory["knowledge"] == "created"

    def test_child_state_has_correct_parent_id(self):
        """Child state created by run() has parent_state_id == orchestrator state_id."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        captured_child = {}

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured_child["state"] = child_state
            return "ok"

        runner = SubagentRunner(ctx, orch, cfg)

        with patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()):
            runner.run("subagent_worker", {"task": "test"}, fake_run_agent)

        assert captured_child["state"].parent_state_id == orch.state_id

    def test_child_state_saved_to_storage(self):
        """Child state should be saved via save_state before run_agent_func is called."""
        from persistence.state_storage import state_storage

        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        captured_child = {}

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured_child["state"] = child_state
            return "ok"

        runner = SubagentRunner(ctx, orch, cfg)

        with patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()):
            runner.run("subagent_worker", {"task": "test"}, fake_run_agent)

        child = captured_child["state"]
        assert state_storage.get_state_by_id(child.state_id) is child

    def test_unknown_subagent_returns_error(self):
        """run() with unknown tool_name returns error string."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = MagicMock()

        runner = SubagentRunner(ctx, orch, cfg)
        result = runner.run("nonexistent_agent", {"task": "test"}, run_agent_func)

        run_agent_func.assert_not_called()
        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "subagent_unknown_tool"

    def test_create_instance_called_with_task(self):
        """run() passes task from arguments to create_instance."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = MagicMock(return_value="ok")

        runner = SubagentRunner(ctx, orch, cfg)

        with (
            patch.object(runner_module, "get_subagent_class") as mock_get_cls,
            patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()),
        ):
            mock_instance = MagicMock()
            mock_instance.get_initial_data.return_value = ("role text", "task text")

            mock_cls = MagicMock()
            mock_cls.create_instance = AsyncMock(return_value=mock_instance)
            mock_get_cls.return_value = mock_cls

            runner.run("subagent_worker", {"task": "build feature", "reason": "r"}, run_agent_func)

        mock_cls.create_instance.assert_awaited_once()
        call_kwargs = mock_cls.create_instance.call_args
        assert call_kwargs.kwargs.get("task") == "build feature"
