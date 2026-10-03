"""Tests for SubagentRunner.run — full subagent lifecycle."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gc_core.states import SharedStateData, State
from gc_core.tools.subagents import runner as runner_module
from gc_core.tools.subagents.runner import SubagentRunner
from gc_testkit.settings import fake_ctx


def _make_orchestrator_state(
    system_prompt="System\n<specific_role>x</specific_role>",
):
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "fs_read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt=system_prompt,
        shared=SharedStateData(working_memory={"state": "initial"}),
    )
    state.backend_api_mode = "chat_completions"
    return state


def _make_config(**overrides):
    cfg = {
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "skills_data_path": "",
        "model": "test-model",
        "system_prompt_id": "test-prompt",
        "subagent_max_steps": 30,
    }
    cfg.update(overrides)
    return cfg


def _make_ctx(config=None):
    return fake_ctx(
        config or _make_config(),
        auth_connector=None,
        logger=None,
        logging_path="",
        summary_trace_path="",
        tools_data={},
        tools_spec=[],
        tools_description=[],
    )


class TestSubagentRunnerRun:
    async def test_run_calls_run_agent_func(self):
        """run() should call run_agent_func with (messages, child_state, child_ctx)."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = AsyncMock(return_value="done")

        runner = SubagentRunner(ctx, orch)

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            result = await runner.arun(
                "subagent_worker",
                {"task": "do X", "reason": "test"},
                run_agent_func,
            )

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

    async def test_run_returns_string_result(self):
        """run() returns whatever run_agent_func returns (as string)."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = AsyncMock(return_value="Task completed successfully")

        runner = SubagentRunner(ctx, orch)

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            result = await runner.arun(
                "subagent_worker", {"task": "test"}, run_agent_func
            )

        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["result"] == "Task completed successfully"

    async def test_run_returns_fallback_on_none(self):
        """run() returns fallback message if run_agent_func returns None."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = AsyncMock(return_value=None)

        runner = SubagentRunner(ctx, orch)

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            result = await runner.arun(
                "subagent_worker", {"task": "test"}, run_agent_func
            )

        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["status"] == "cancelled_by_user"
        assert payload["meta"]["finish_reason"] == "stopped_by_user"
        assert payload["error"]["code"] == "cancelled_by_user"
        assert "interrupted" in payload["error"]["message"].lower()

    async def test_shared_mutations_visible_to_orchestrator(self):
        """Subagent mutations on shared.working_memory are visible in orchestrator state."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            child_state.shared.working_memory["state"] = "updated by subagent"
            child_state.shared.working_memory["knowledge"] = "created"
            return "Subagent done"

        runner = SubagentRunner(ctx, orch)

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            await runner.arun(
                "subagent_worker", {"task": "modify memory"}, fake_run_agent
            )

        assert orch.shared.working_memory["state"] == "updated by subagent"
        assert orch.shared.working_memory["knowledge"] == "created"

    async def test_child_state_has_correct_parent_id(self):
        """Child state created by run() has parent_state_id == orchestrator state_id."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        captured_child = {}

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured_child["state"] = child_state
            return "ok"

        runner = SubagentRunner(ctx, orch)

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            await runner.arun(
                "subagent_worker", {"task": "test"}, fake_run_agent
            )

        assert captured_child["state"].parent_state_id == orch.state_id

    async def test_child_state_passed_to_run_agent(self):
        """Child state is created and passed to run_agent_func before execution."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        captured_child = {}

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured_child["state"] = child_state
            return "ok"

        runner = SubagentRunner(ctx, orch)

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            await runner.arun(
                "subagent_worker", {"task": "test"}, fake_run_agent
            )

        child = captured_child["state"]
        assert isinstance(child, State)
        assert child.parent_state_id == orch.state_id

    async def test_unknown_subagent_returns_error(self):
        """run() with unknown tool_name returns error string."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = AsyncMock()

        runner = SubagentRunner(ctx, orch)
        result = await runner.arun(
            "nonexistent_agent", {"task": "test"}, run_agent_func
        )

        run_agent_func.assert_not_called()
        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "subagent_unknown_tool"

    async def test_create_instance_called_with_task(self):
        """run() passes task from arguments to create_instance."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = AsyncMock(return_value="ok")

        runner = SubagentRunner(ctx, orch)

        with (
            patch.object(runner_module, "get_subagent_class") as mock_get_cls,
            patch.object(
                runner, "_build_child_ctx", return_value=SimpleNamespace()
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.get_initial_data.return_value = (
                "role text",
                "task text",
            )

            mock_cls = MagicMock()
            mock_cls.create_instance = AsyncMock(return_value=mock_instance)
            mock_get_cls.return_value = mock_cls

            await runner.arun(
                "subagent_worker",
                {"task": "build feature", "reason": "r"},
                run_agent_func,
            )

        mock_cls.create_instance.assert_awaited_once()
        call_kwargs = mock_cls.create_instance.call_args
        assert call_kwargs.kwargs.get("task") == "build feature"
