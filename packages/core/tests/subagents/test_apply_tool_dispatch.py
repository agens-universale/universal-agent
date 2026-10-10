"""Tests for apply_tool subagent dispatch logic."""

import json
from unittest.mock import MagicMock, patch

import pytest
from gc_core.states import SharedStateData, State
from gc_core.tools import WrongFunctionArgumentsException, aapply_tool
from gc_testkit.settings import fake_ctx


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


def _make_ctx(tools_data, use_local=True):
    cfg = {
        "agent.use_local_subagents": use_local,
        "backend.api_mode": "chat_completions",
        "paths.skills_data_path": "",
        "agent.model": "test-model",
        "agent.system_prompt_id": "test-prompt",
    }
    return fake_ctx(
        cfg,
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
    from gc_core.tools.subagents.registry import get_subagents_tools_data

    return get_subagents_tools_data()


class TestSubagentDispatch:
    async def test_subagent_call_reaches_runner(self):
        """apply_tool delegates subagent_worker to SubagentRunner.run."""
        sub_data = _subagent_tools_data()
        ctx = _make_ctx(sub_data, use_local=True)
        state = _make_state()
        run_agent_func = MagicMock(return_value="subagent result")

        with patch(
            "gc_core.tools.subagents.runner.SubagentRunner"
        ) as MockRunner:
            instance = MockRunner.return_value

            # `arun` корутинный: обычный return_value отдал бы MagicMock, и
            # `await` над ним падал бы, а не отдавал результат.
            async def _arun(*_a, **_kw):
                return "subagent result"

            instance.arun.side_effect = _arun

            result = await aapply_tool(
                tool_name="subagent_worker",
                arguments=json.dumps(
                    {"task": "do something", "reason": "test"}
                ),
                ctx=ctx,
                state=state,
                run_agent_func=run_agent_func,
            )

        MockRunner.assert_called_once_with(ctx, state, ctx)
        instance.arun.assert_called_once_with(
            "subagent_worker",
            {"task": "do something", "reason": "test"},
            run_agent_func,
            parent_call_id=None,
        )
        assert result == "subagent result"

    async def test_subagent_call_with_dict_arguments(self):
        """apply_tool handles dict arguments for subagent calls."""
        sub_data = _subagent_tools_data()
        ctx = _make_ctx(sub_data, use_local=True)
        state = _make_state()
        run_agent_func = MagicMock()

        with patch(
            "gc_core.tools.subagents.runner.SubagentRunner"
        ) as MockRunner:
            instance = MockRunner.return_value

            async def _arun(*_a, **_kw):
                return "ok"

            instance.arun.side_effect = _arun

            await aapply_tool(
                tool_name="subagent_worker",
                arguments={"task": "hello", "reason": "test"},
                ctx=ctx,
                state=state,
                run_agent_func=run_agent_func,
            )

        instance.arun.assert_called_once_with(
            "subagent_worker",
            {"task": "hello", "reason": "test"},
            run_agent_func,
            parent_call_id=None,
        )

    async def test_regular_tool_not_affected(self):
        """Non-subagent tools go through normal tool_fabric path."""
        called = {}

        class FakeTool:
            def __init__(self, **kw):
                called["kwargs"] = kw

            async def acall(self, path=""):
                return f"read: {path}"

        tools_data = {
            "fs_read_file": {
                "spec": {
                    "name": "fs_read_file",
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

        result = await aapply_tool(
            tool_name="fs_read_file",
            arguments=json.dumps({"path": "/tmp/test.txt"}),
            ctx=ctx,
            state=state,
        )
        assert result == "read: /tmp/test.txt"

    async def test_regular_tool_receives_ctx(self):
        """
        Regression: apply_tool must propagate ctx to tool_fabric kwargs so that
        security decorators (read_guard, root_box, url_policy,
        command_moderation) resolve policy via self._ctx settings. Without
        this, the decorators see no settings and treat the layer as not
        configured — a bug that previously caused ~49 silent-allow FAILs in
        rootbox_test_cases_100_v1 (run_20260520T074244Z). Плоский
        ``config_dict`` тулу больше не передаётся: настройки — только из ctx.
        """
        captured = {}

        class FakeTool:
            def __init__(self, **kw):
                captured["kwargs"] = kw

            async def acall(self, path=""):
                return "ok"

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
        ctx = _make_ctx(tools_data, use_local=False)
        state = _make_state()

        await aapply_tool(
            tool_name="read_file",
            arguments=json.dumps({"path": "/tmp/x"}),
            ctx=ctx,
            state=state,
        )

        assert captured["kwargs"].get("ctx") is ctx
        assert "config_dict" not in captured["kwargs"]

    async def test_subagent_with_local_disabled_gives_no_such_tool(self):
        """When use_local_subagents=False, subagent tool_name is unknown."""
        tools_data = {}
        ctx = _make_ctx(tools_data, use_local=False)
        state = _make_state()

        result = await aapply_tool(
            tool_name="subagent_worker",
            arguments=json.dumps({"task": "test"}),
            ctx=ctx,
            state=state,
        )
        assert "not such tool" in result or "no" in result.lower()

    async def test_missing_state_returns_error(self):
        """Subagent dispatch without state kwarg returns error string."""
        sub_data = _subagent_tools_data()
        ctx = _make_ctx(sub_data, use_local=True)

        result = await aapply_tool(
            tool_name="subagent_worker",
            arguments=json.dumps({"task": "test"}),
            ctx=ctx,
        )
        assert "missing" in result.lower() or "state" in result.lower()

    async def test_missing_run_agent_func_returns_error(self):
        """Subagent dispatch without run_agent_func returns error string."""
        sub_data = _subagent_tools_data()
        ctx = _make_ctx(sub_data, use_local=True)
        state = _make_state()

        result = await aapply_tool(
            tool_name="subagent_worker",
            arguments=json.dumps({"task": "test"}),
            ctx=ctx,
            state=state,
        )
        assert "missing" in result.lower() or "run_agent" in result.lower()

    async def test_invalid_subagent_arguments_raise_validation_error(self):
        """Subagent args are schema-validated before runner execution."""
        sub_data = _subagent_tools_data()
        ctx = _make_ctx(sub_data, use_local=True)
        state = _make_state()
        run_agent_func = MagicMock()

        # Для subagent_worker обязательны reason и task.
        with patch(
            "gc_core.tools.subagents.runner.SubagentRunner"
        ) as MockRunner:
            with pytest.raises(WrongFunctionArgumentsException):
                await aapply_tool(
                    tool_name="subagent_worker",
                    arguments=json.dumps({"task": "only task"}),
                    ctx=ctx,
                    state=state,
                    run_agent_func=run_agent_func,
                )
            MockRunner.assert_not_called()
