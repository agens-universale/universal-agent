"""Tests for SpecialAgent create_instance/finalize_role and integration with SubagentRunner."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from gc_core.config_modules.chat_completions import (
    DEFAULT_SUBAGENT_ROLE_MAX_TOKENS,
    ChatCompletionsLlmConfig,
)
from gc_core.states import State, SharedStateData
from gc_core.tools.subagents import special_agent as special_agent_module
from gc_core.tools.subagents.special_agent import SpecialAgent
from gc_core.tools.subagents import runner as runner_module
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
        "completions_url": "http://fake-llm/v1/chat/completions",
        "use_ssl": False,
        "backend_request_timeout_sec": 600,
        "chat_completion_headers": {"Authorization": "Bearer test-token"},
    }
    cfg.update(overrides)
    return cfg


def _make_ctx(cfg=None):
    return SimpleNamespace(
        config_dict=cfg or _make_config(),
        auxiliary_llm=ChatCompletionsLlmConfig(),
        auth_connector=None,
        logger=None,
        logging_path="",
        summary_trace_path="",
        tools_data={},
        tools_spec=[],
        tools_description=[],
        model_router=None,
        is_tui=False,
    )


def _make_state(model="test-model"):
    return SimpleNamespace(default_model=model, usage=None, state_id="s1", x_target=None)


class TestSpecialAgentCreateInstance:
    def test_create_instance_does_not_generate_role(self):
        """create_instance больше не делает LLM-вызов — agent_role резолвится позже,
        через finalize_role (см. TestFinalizeRole)."""
        instance = asyncio.run(
            SpecialAgent.create_instance(
                role="Python Dev",
                role_description="Expert in Python",
                task="Write a function",
                model="test-model",
                config_dict=_make_config(),
            )
        )
        assert instance.agent_role is None
        assert instance.task == "Write a function"


class TestFinalizeRole:
    def test_finalize_role_generates_agent_role(self):
        instance = asyncio.run(
            SpecialAgent.create_instance(
                role="Analyst",
                role_description="Data analyst",
                task="Analyze data",
                model="test-model",
                config_dict=_make_config(),
            )
        )
        with patch.object(
            special_agent_module, "request_auxiliary_completion_text",
            return_value="Custom role",
        ):
            instance.finalize_role(_make_ctx(), _make_state())

        agent_role, task = instance.get_initial_data()
        assert agent_role.startswith("Custom role")
        assert "Analyze data" in task

    def test_llm_receives_role_in_prompt(self):
        """The LLM prompt should contain role name and description."""
        instance = asyncio.run(
            SpecialAgent.create_instance(
                role="Security Expert",
                role_description="Specializes in web security",
                task="Audit the app",
                model="test-model",
                config_dict=_make_config(),
            )
        )
        with patch.object(
            special_agent_module, "request_auxiliary_completion_text",
            return_value="ok",
        ) as mock_call:
            instance.finalize_role(_make_ctx(), _make_state())

        user_msg = mock_call.call_args.kwargs["messages"][1]["content"]
        assert "Security Expert" in user_msg
        assert "Specializes in web security" in user_msg

    def test_declares_own_consumer(self):
        """Bearer-регресс (спека Task 7): генерация роли идёт через общую воронку
        с consumer=subagent:subagent_special_agent — резолв авторизации/роутера
        общий для всех вспомогательных LLM-вызовов."""
        instance = asyncio.run(
            SpecialAgent.create_instance(
                role="Dev", role_description="desc", task="task",
                model="test-model", config_dict=_make_config(),
            )
        )
        ctx = _make_ctx()
        state = _make_state()
        with patch.object(
            special_agent_module, "request_auxiliary_completion_text",
            return_value="ok",
        ) as mock_call:
            instance.finalize_role(ctx, state)

        kwargs = mock_call.call_args.kwargs
        assert kwargs["consumer"] == "subagent:subagent_special_agent"
        assert kwargs["ctx"] is ctx
        assert kwargs["state"] is state
        assert kwargs["request_label"] == "subagent_special_agent"

    def test_uses_configured_max_tokens_default(self):
        instance = asyncio.run(
            SpecialAgent.create_instance(
                role="Dev", role_description="desc", task="task",
                model="test-model", config_dict=_make_config(),
            )
        )
        with patch.object(
            special_agent_module, "request_auxiliary_completion_text",
            return_value="ok",
        ) as mock_call:
            instance.finalize_role(_make_ctx(), _make_state())

        assert mock_call.call_args.kwargs["max_tokens"] == DEFAULT_SUBAGENT_ROLE_MAX_TOKENS

    def test_generate_role_failure_propagates(self):
        """If LLM call fails, finalize_role should raise."""
        instance = asyncio.run(
            SpecialAgent.create_instance(
                role="Dev", role_description="desc", task="task",
                model="m", config_dict=_make_config(),
            )
        )
        with patch.object(
            special_agent_module, "request_auxiliary_completion_text",
            side_effect=ConnectionError("Network error"),
        ):
            with pytest.raises(ConnectionError):
                instance.finalize_role(_make_ctx(), _make_state())


class TestSpecialAgentWithRunner:
    def test_runner_calls_special_agent_end_to_end(self):
        """SubagentRunner.run integrates with SpecialAgent correctly: role generation
        now happens after child ctx is built, via finalize_role."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = MagicMock(return_value="Special agent done")

        runner = SubagentRunner(ctx, orch, cfg)

        with (
            patch.object(
                special_agent_module, "request_auxiliary_completion_text",
                return_value="You are a security auditor.",
            ),
            patch.object(
                runner, "_build_child_ctx",
                return_value=SimpleNamespace(
                    logger=None,
                    run_id="cr",
                    is_tui=False,
                    auxiliary_llm=ChatCompletionsLlmConfig(),
                    config_dict=cfg,
                ),
            ),
        ):
            result = runner.run(
                "subagent_special_agent",
                {
                    "reason": "Need security audit",
                    "role": "Security Auditor",
                    "role_description": "Expert in web app security",
                    "task": "Audit the authentication module",
                },
                run_agent_func,
            )

        parsed = json.loads(result)
        assert parsed["ok"] is True
        assert parsed["result"] == "Special agent done"
        run_agent_func.assert_called_once()

        call_args = run_agent_func.call_args
        messages = call_args[0][0]
        child_state = call_args[0][1]

        user_msgs = [m for m in messages if m["role"] == "user"]
        assert any("Audit the authentication module" in m["content"] for m in user_msgs)
        assert child_state.parent_state_id == orch.state_id
        assert child_state.specific_role.startswith("You are a security auditor.")
