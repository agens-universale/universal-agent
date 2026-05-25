"""Tests for SpecialAgent create_instance and integration with SubagentRunner."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from states import State, SharedStateData, save_state
from tools.subagents import special_agent as special_agent_module
from tools.subagents.special_agent import SpecialAgent
from tools.subagents import runner as runner_module
from tools.subagents.runner import SubagentRunner


def _make_orchestrator_state():
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="System\n<specific_role>x</specific_role>",
        shared=SharedStateData(),
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
        "completions_url": "http://fake-llm/v1/chat/completions",
        "use_ssl": False,
        "chat_completion_headers": {"Authorization": "Bearer test-token"},
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


def _mock_llm_response(content="You are an expert Python developer."):
    return {"choices": [{"message": {"content": content}}]}


class TestSpecialAgentCreateInstance:
    def test_create_instance_calls_generate_role(self):
        """create_instance generates agent_role via LLM call."""
        with patch.object(
            special_agent_module, "request_backend_chat_completions",
            return_value=_mock_llm_response("Generated role prompt"),
        ):
            instance = asyncio.run(
                SpecialAgent.create_instance(
                    role="Python Dev",
                    role_description="Expert in Python",
                    task="Write a function",
                    model="test-model",
                    config_dict=_make_config(),
                )
            )

        assert instance.agent_role == "Generated role prompt"
        assert instance.task == "Write a function"

    def test_get_initial_data_returns_generated_role_and_task(self):
        with patch.object(
            special_agent_module, "request_backend_chat_completions",
            return_value=_mock_llm_response("Custom role"),
        ):
            instance = asyncio.run(
                SpecialAgent.create_instance(
                    role="Analyst",
                    role_description="Data analyst",
                    task="Analyze data",
                    model="test-model",
                    config_dict=_make_config(),
                )
            )

        agent_role, task = instance.get_initial_data()
        assert agent_role.startswith("Custom role")
        assert "Analyze data" in task

    def test_llm_receives_role_in_prompt(self):
        """The LLM prompt should contain role name and description."""
        with patch.object(
            special_agent_module, "request_backend_chat_completions",
            return_value=_mock_llm_response(),
        ) as mock_llm:
            asyncio.run(
                SpecialAgent.create_instance(
                    role="Security Expert",
                    role_description="Specializes in web security",
                    task="Audit the app",
                    model="test-model",
                    config_dict=_make_config(),
                )
            )

        call_data = mock_llm.call_args[0][0]
        user_msg = call_data["messages"][1]["content"]
        assert "Security Expert" in user_msg
        assert "Specializes in web security" in user_msg

    def test_model_passed_to_llm_call(self):
        """The model used for role generation should be the orchestrator's model."""
        with patch.object(
            special_agent_module, "request_backend_chat_completions",
            return_value=_mock_llm_response(),
        ) as mock_llm:
            asyncio.run(
                SpecialAgent.create_instance(
                    role="Dev",
                    role_description="desc",
                    task="task",
                    model="gpt-4o-mini",
                    config_dict=_make_config(),
                )
            )

        call_data = mock_llm.call_args[0][0]
        assert call_data["model"] == "gpt-4o-mini"

    def test_config_dict_used_for_url_and_auth(self):
        """URL and Authorization header from config_dict should be passed to the LLM call."""
        cfg = _make_config(
            completions_url="http://custom-url/v1/chat/completions",
            chat_completion_headers={"Authorization": "Bearer my-secret-token"},
            use_ssl=False,
        )
        with patch.object(
            special_agent_module, "request_backend_chat_completions",
            return_value=_mock_llm_response(),
        ) as mock_llm:
            asyncio.run(
                SpecialAgent.create_instance(
                    role="Dev",
                    role_description="desc",
                    task="task",
                    model="m",
                    config_dict=cfg,
                )
            )

        _, headers = mock_llm.call_args[0]
        kwargs = mock_llm.call_args[1]
        assert headers.get("Authorization") == "Bearer my-secret-token"
        assert kwargs.get("url") == "http://custom-url/v1/chat/completions"

    def test_generate_role_failure_propagates(self):
        """If LLM call fails, create_instance should raise."""
        with patch.object(
            special_agent_module, "request_backend_chat_completions",
            side_effect=ConnectionError("Network error"),
        ):
            with pytest.raises(ConnectionError):
                asyncio.run(
                    SpecialAgent.create_instance(
                        role="Dev",
                        role_description="desc",
                        task="task",
                        model="m",
                        config_dict=_make_config(),
                    )
                )


class TestSpecialAgentWithRunner:
    def test_runner_calls_special_agent_end_to_end(self):
        """SubagentRunner.run integrates with SpecialAgent correctly."""
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        run_agent_func = MagicMock(return_value="Special agent done")

        runner = SubagentRunner(ctx, orch, cfg)

        with (
            patch.object(
                special_agent_module, "request_backend_chat_completions",
                return_value=_mock_llm_response("You are a security auditor."),
            ),
            patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()),
        ):
            result = runner.run(
                "system_subagent_special_agent",
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
