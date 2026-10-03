"""Tests for error handling in SubagentRunner.run()."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gc_core.states import SharedStateData, State
from gc_core.tools.subagents import runner as runner_module
from gc_core.tools.subagents.runner import SubagentRunner
from gc_testkit.settings import fake_ctx


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
        shared=SharedStateData(working_memory={"state": "before"}),
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
    }
    cfg.update(overrides)
    return cfg


def _make_ctx(cfg=None):
    return fake_ctx(
        cfg or _make_config(),
        auth_connector=None,
        logger=None,
        logging_path="",
        summary_trace_path="",
        tools_data={},
        tools_spec=[],
        tools_description=[],
    )


class TestCreateInstanceError:
    async def test_network_error_returns_error_string(self):
        """ConnectionError in create_instance should not crash the orchestrator."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch)

        with patch.object(runner_module, "get_subagent_class") as mock_cls:
            cls = MagicMock()
            cls.create_instance = AsyncMock(
                side_effect=ConnectionError("LLM unreachable")
            )
            mock_cls.return_value = cls

            result = await runner.arun(
                "subagent_worker", {"task": "test"}, MagicMock()
            )

        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "subagent_create_failed"

    async def test_generic_exception_returns_error_string(self):
        """Any exception in create_instance is caught and returns error string."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch)

        with patch.object(runner_module, "get_subagent_class") as mock_cls:
            cls = MagicMock()
            cls.create_instance = AsyncMock(side_effect=ValueError("bad args"))
            mock_cls.return_value = cls

            result = await runner.arun(
                "subagent_worker", {"task": "test"}, MagicMock()
            )

        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "subagent_create_failed"

    async def test_orchestrator_not_crashed(self):
        """Orchestrator state remains intact after create_instance failure."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch)

        with patch.object(runner_module, "get_subagent_class") as mock_cls:
            cls = MagicMock()
            cls.create_instance = AsyncMock(side_effect=RuntimeError("boom"))
            mock_cls.return_value = cls

            await runner.arun("subagent_worker", {"task": "test"}, MagicMock())

        assert orch.shared.working_memory["state"] == "before"
        assert orch.stop_requested is False


class TestBuildChildCtxError:
    async def test_ctx_build_failure_returns_error_string(self):
        """Error in _build_child_ctx should not crash the orchestrator."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch)

        with patch.object(
            runner, "_build_child_ctx", side_effect=ImportError("no module")
        ):
            result = await runner.arun(
                "subagent_worker",
                {"task": "test"},
                MagicMock(),
            )

        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "subagent_ctx_build_failed"


class TestRunAgentError:
    async def test_run_agent_exception_returns_error_string(self):
        """Exception in run_agent_func should return error, not crash."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch)
        run_agent_func = MagicMock(
            side_effect=RuntimeError("LLM backend down")
        )

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            result = await runner.arun(
                "subagent_worker", {"task": "test"}, run_agent_func
            )

        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "subagent_run_failed"

    async def test_error_message_contains_tool_name(self):
        """Error string should mention which subagent failed."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch)
        run_agent_func = MagicMock(side_effect=Exception("fail"))

        with patch.object(
            runner, "_build_child_ctx", return_value=SimpleNamespace()
        ):
            result = await runner.arun(
                "subagent_worker", {"task": "test"}, run_agent_func
            )

        payload = json.loads(result)
        assert payload["meta"]["tool_name"] == "subagent_worker"
