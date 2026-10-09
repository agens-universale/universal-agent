"""Канальные дефолты: проза allow, finish-gate off, наджи todo выключены."""

from pathlib import Path

import pytest
from gc_chat.agent_config import prepare_agent_settings
from gc_chat.completions.prompt_assets import (
    completions_special_instructions_path,
)
from gc_chat.main import apply_chat_runtime_defaults, backend_rejection
from gc_chat.responses.prompt_assets import responses_special_instructions_path
from gc_core.config_modules.chat_api import ChatApiConfig
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
)
from gc_testkit.settings import make_settings


def test_defaults_allow_and_finish_gate_off():
    base = make_settings(
        **{
            "agent.strict_turn_finish_validation": True,
            "agent.max_steps": 7,
            "todo.reminder_steps": 4,
            "text_answer_mode": "deny",
            "finish_gate_smart": True,
        }
    )
    loaded = apply_chat_runtime_defaults(base)
    assert loaded.runtime.text_answer_mode == "allow"
    assert loaded.settings.agent.strict_turn_finish_validation is False
    assert loaded.runtime.finish_gate_smart is False
    assert loaded.settings.todo.reminder_steps == 0
    # Правка точечная: соседние ключи остаются.
    assert loaded.settings.agent.max_steps == 7
    stop_tools = loaded.settings.agent.stop_tools
    assert ASK_HUMAN_TOOL_NAME in stop_tools
    assert INTERMEDIATE_REPLY_TOOL_NAME in stop_tools
    assert "session_todo" not in stop_tools
    # Исходные настройки не меняются: дефолты — новый объект.
    assert base.settings.agent.strict_turn_finish_validation is True
    assert base.settings.todo.reminder_steps == 4
    assert base.runtime.text_answer_mode == "deny"
    assert base.settings.agent.stop_tools == []


def test_channel_keys_win():
    loaded = apply_chat_runtime_defaults(
        make_settings(
            **{
                "chat_api.text_answer_mode": "judge",
                "chat_api.finish_gate_mode": "smart",
                "chat_api.todo_reminder_steps": 3,
                "agent.strict_turn_finish_validation": False,
            }
        )
    )
    assert loaded.runtime.text_answer_mode == "judge"
    assert loaded.settings.agent.strict_turn_finish_validation is True
    assert loaded.runtime.finish_gate_smart is True
    assert loaded.settings.todo.reminder_steps == 3


def test_finish_gate_on_overrides_agent_false():
    loaded = apply_chat_runtime_defaults(
        make_settings(
            **{
                "chat_api.finish_gate_mode": "on",
                "agent.strict_turn_finish_validation": False,
                "todo.reminder_steps": 2,
            }
        )
    )
    assert loaded.settings.agent.strict_turn_finish_validation is True
    assert loaded.runtime.finish_gate_smart is False
    assert loaded.settings.todo.reminder_steps == 0


def test_yaml_section_forbids_unknown_and_loads(tmp_path):
    with pytest.raises(Exception):
        ChatApiConfig(todo_nudge=1)
    cfg = ChatApiConfig(
        todo_reminder_steps=5,
        text_answer_mode="deny",
        finish_gate_mode="off",
        cors_origins=["http://ui", "*"],
    )
    assert cfg.text_answer_mode == "deny"
    assert cfg.todo_reminder_steps == 5
    assert cfg.cors_origins == ["http://ui"]
    from gc_core.config import load_settings

    path = tmp_path / "config.yml"
    path.write_text(
        "agent:\n"
        "  strict_turn_finish_validation: true\n"
        "chat_api:\n"
        "  text_answer_mode: allow\n"
        '  finish_gate_mode: "off"\n'
        "  todo_reminder_steps: 0\n",
        encoding="utf-8",
    )
    loaded = load_settings(str(path))
    assert loaded.settings.chat_api.todo_reminder_steps == 0
    assert loaded.settings.agent.strict_turn_finish_validation is True
    loaded = apply_chat_runtime_defaults(loaded)
    assert loaded.runtime.text_answer_mode == "allow"
    assert loaded.settings.agent.strict_turn_finish_validation is False
    assert loaded.runtime.finish_gate_smart is False
    assert loaded.settings.todo.reminder_steps == 0
    assert loaded.settings.chat_api.host == "127.0.0.1"
    assert loaded.settings.chat_api.port == 8090


def test_ask_human_is_stopped_without_stamping_the_prompt():
    base = make_settings(
        **{
            "agent.stop_tools": [
                "exec_command",
                ASK_HUMAN_TOOL_NAME,
                INTERMEDIATE_REPLY_TOOL_NAME,
            ]
        }
    )
    loaded = apply_chat_runtime_defaults(base)
    stop_tools = loaded.settings.agent.stop_tools
    assert stop_tools.count(ASK_HUMAN_TOOL_NAME) == 1
    assert stop_tools.count(INTERMEDIATE_REPLY_TOOL_NAME) == 1
    assert stop_tools[0] == "exec_command"
    assert (
        loaded.settings.paths.chat_completions_special_instructions_path
        == base.settings.paths.chat_completions_special_instructions_path
    )


def test_own_stop_tools_survive_the_channel_stop():
    """Правка заменяет список целиком: свои запреты конфига не теряются."""
    loaded = apply_chat_runtime_defaults(
        make_settings(**{"agent.stop_tools": ["exec_command", "web_fetch"]})
    )
    assert loaded.settings.agent.stop_tools == [
        "exec_command",
        "web_fetch",
        ASK_HUMAN_TOOL_NAME,
        INTERMEDIATE_REPLY_TOOL_NAME,
    ]


def test_each_channel_stamps_its_own_prompt_on_a_copy():
    base = make_settings(launch_root="process")
    base_prompt = (
        base.settings.paths.chat_completions_special_instructions_path
    )
    responses = prepare_agent_settings(
        base,
        cwd="/work",
        instructions_path=responses_special_instructions_path(),
    )
    completions = prepare_agent_settings(
        base,
        cwd="/work",
        instructions_path=completions_special_instructions_path(),
    )
    assert (
        base.settings.paths.chat_completions_special_instructions_path
        == base_prompt
    )
    assert base.runtime.launch_root == "process"
    assert responses.runtime.launch_root == "/work"
    assert completions.runtime.launch_root == "/work"
    responses_path = (
        responses.settings.paths.chat_completions_special_instructions_path
    )
    completions_path = (
        completions.settings.paths.chat_completions_special_instructions_path
    )
    responses_text = Path(responses_path).read_text(encoding="utf-8")
    completions_text = Path(completions_path).read_text(encoding="utf-8")
    assert "prose" in responses_text
    assert "Do not call a tool to ask" in responses_text
    assert "<think>" in completions_text
    assert "final prose" in completions_text
    assert responses_path != completions_path


def test_ask_human_stop_does_not_require_prior_list():
    loaded = apply_chat_runtime_defaults(make_settings())
    assert loaded.settings.agent.stop_tools == [
        ASK_HUMAN_TOOL_NAME,
        INTERMEDIATE_REPLY_TOOL_NAME,
    ]


def test_harbor_and_local_backend():
    assert backend_rejection(make_settings()) is None
    assert backend_rejection(make_settings(os_backend="local")) is None
    text = backend_rejection(make_settings(os_backend="harbor"))
    assert text is not None
    assert "harbor" in text
    rejected = backend_rejection(make_settings(os_backend="docker"))
    assert rejected is not None
    assert "docker" in rejected
