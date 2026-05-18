"""Tests for apply_tool subagent dispatch logic."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from states import State, SharedStateData, save_state
from tools import apply_tool, WrongFunctionArgumentsException


def _make_state():
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="You are a helpful assistant.",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    save_state(state)
    return state


def _make_ctx(tools_data, use_local=True):
    cfg = {
        "use_local_subagents": use_local,
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
    )


def _subagent_tools_data():
    """Minimal tools_data entry for a subagent."""
    from tools.subagents.registry import get_subagents_tools_data
    return get_subagents_tools_data()


class TestSubagentDispatch:
    def test_subagent_call_reaches_runner(self):
        """apply_tool delegates subagent_worker to SubagentRunner.run."""
        sub_data = _subagent_tools_data()
        ctx = _make_ctx(sub_data, use_local=True)
        state = _make_state()
        run_agent_func = MagicMock(return_value="subagent result")

        with patch("tools.subagents.runner.SubagentRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.run.return_value = "subagent result"

            result = apply_tool(
                tool_name="subagent_worker",
                arguments=json.dumps({"task": "do something", "reason": "test"}),
                ctx=ctx,
                state=state,
                run_agent_func=run_agent_func,
            )

        MockRunner.assert_called_once_with(ctx, state, ctx.config_dict)
        instance.run.assert_called_once_with(
            "subagent_worker",
            {"task": "do something", "reason": "test"},
            run_agent_func,
        )
        assert result == "subagent result"

    def test_subagent_call_with_dict_arguments(self):
        """apply_tool handles dict arguments for subagent calls."""
        sub_data = _subagent_tools_data()
        ctx = _make_ctx(sub_data, use_local=True)
        state = _make_state()
        run_agent_func = MagicMock()

        with patch("tools.subagents.runner.SubagentRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.run.return_value = "ok"

            apply_tool(
                tool_name="subagent_worker",
                arguments={"task": "hello", "reason": "test"},
                ctx=ctx,
                state=state,
                run_agent_func=run_agent_func,
            )

        instance.run.assert_called_once_with(
            "subagent_worker", {"task": "hello", "reason": "test"}, run_agent_func,
        )

    def test_regular_tool_not_affected(self):
        """Non-subagent tools go through normal tool_fabric path."""
        called = {}

        class FakeTool:
            def __init__(self, **kw):
                called["kwargs"] = kw

            def __call__(self, path=""):
                return f"read: {path}"

        tools_data = {
            "read_file": {
                "spec": {
                    "name": "read_file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
                "description": "Read a file",
                "tool_fabric": FakeTool,
            }
        }
        ctx = _make_ctx(tools_data, use_local=True)
        state = _make_state()

        result = apply_tool(
            tool_name="read_file",
            arguments=json.dumps({"path": "/tmp/test.txt"}),
            ctx=ctx,
            state=state,
        )
        assert result == "read: /tmp/test.txt"

    def test_subagent_with_local_disabled_gives_no_such_tool(self):
        """When use_local_subagents=False, subagent tool_name is unknown."""
        tools_data = {}
        ctx = _make_ctx(tools_data, use_local=False)
        state = _make_state()

        result = apply_tool(
            tool_name="subagent_worker",
            arguments=json.dumps({"task": "test"}),
            ctx=ctx,
            state=state,
        )
        assert "not such tool" in result or "no" in result.lower()

    def test_missing_state_returns_error(self):
        """Subagent dispatch without state kwarg returns error string."""
        sub_data = _subagent_tools_data()
        ctx = _make_ctx(sub_data, use_local=True)

        result = apply_tool(
            tool_name="subagent_worker",
            arguments=json.dumps({"task": "test"}),
            ctx=ctx,
        )
        assert "missing" in result.lower() or "state" in result.lower()

    def test_missing_run_agent_func_returns_error(self):
        """Subagent dispatch without run_agent_func returns error string."""
        sub_data = _subagent_tools_data()
        ctx = _make_ctx(sub_data, use_local=True)
        state = _make_state()

        result = apply_tool(
            tool_name="subagent_worker",
            arguments=json.dumps({"task": "test"}),
            ctx=ctx,
            state=state,
        )
        assert "missing" in result.lower() or "run_agent" in result.lower()

    def test_invalid_subagent_arguments_raise_validation_error(self):
        """Subagent args are schema-validated before runner execution."""
        sub_data = _subagent_tools_data()
        ctx = _make_ctx(sub_data, use_local=True)
        state = _make_state()
        run_agent_func = MagicMock()

        # Для subagent_worker обязательны reason и task.
        with patch("tools.subagents.runner.SubagentRunner") as MockRunner:
            with pytest.raises(WrongFunctionArgumentsException):
                apply_tool(
                    tool_name="subagent_worker",
                    arguments=json.dumps({"task": "only task"}),
                    ctx=ctx,
                    state=state,
                    run_agent_func=run_agent_func,
                )
            MockRunner.assert_not_called()
