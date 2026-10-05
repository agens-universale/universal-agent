"""Канальные дефолты: проза allow, finish-gate off, наджи todo выключены."""

from pathlib import Path

import pytest
from gc_chat.agent_config import prepare_agent_config
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


def test_defaults_allow_and_finish_gate_off():
    config = {
        "strict_turn_finish_validation": True,
        "text_answer_mode": "deny",
        "finish_gate_smart": True,
        "todo_params": {"reminder_steps": 4, "other": 1},
    }
    apply_chat_runtime_defaults(config)
    assert config["text_answer_mode"] == "allow"
    assert config["strict_turn_finish_validation"] is False
    assert config["finish_gate_smart"] is False
    assert config["todo_params"]["reminder_steps"] == 0
    assert config["todo_params"]["other"] == 1
    assert ASK_HUMAN_TOOL_NAME in config["stop_tools"]
    assert INTERMEDIATE_REPLY_TOOL_NAME in config["stop_tools"]
    assert "session_todo" not in config["stop_tools"]


def test_channel_keys_win():
    config = {
        "chat_api_text_answer_mode": "judge",
        "chat_api_finish_gate_mode": "smart",
        "chat_api_todo_reminder_steps": 3,
        "strict_turn_finish_validation": False,
    }
    apply_chat_runtime_defaults(config)
    assert config["text_answer_mode"] == "judge"
    assert config["strict_turn_finish_validation"] is True
    assert config["finish_gate_smart"] is True
    assert config["todo_params"]["reminder_steps"] == 3


def test_finish_gate_on_overrides_agent_false():
    config = {
        "chat_api_finish_gate_mode": "on",
        "strict_turn_finish_validation": False,
        "todo_params": {"reminder_steps": 2},
    }
    apply_chat_runtime_defaults(config)
    assert config["strict_turn_finish_validation"] is True
    assert config["finish_gate_smart"] is False
    assert config["todo_params"]["reminder_steps"] == 0


def test_yaml_section_forbids_unknown_and_flattens(tmp_path):
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
    from gc_core import config as cfgmod

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
    loaded = cfgmod.load_config(str(path))
    assert loaded["chat_api_todo_reminder_steps"] == 0
    assert loaded["strict_turn_finish_validation"] is True
    apply_chat_runtime_defaults(loaded)
    assert loaded["text_answer_mode"] == "allow"
    assert loaded["strict_turn_finish_validation"] is False
    assert loaded["finish_gate_smart"] is False
    assert loaded["todo_params"]["reminder_steps"] == 0
    assert loaded["chat_api_host"] == "127.0.0.1"
    assert loaded["chat_api_port"] == 8090


def test_ask_human_is_stopped_without_stamping_the_prompt():
    config = {
        "stop_tools": [
            "exec_command",
            ASK_HUMAN_TOOL_NAME,
            INTERMEDIATE_REPLY_TOOL_NAME,
        ]
    }
    apply_chat_runtime_defaults(config)
    assert config["stop_tools"].count(ASK_HUMAN_TOOL_NAME) == 1
    assert config["stop_tools"].count(INTERMEDIATE_REPLY_TOOL_NAME) == 1
    assert config["stop_tools"][0] == "exec_command"
    assert "chat_completions_special_instructions_path" not in config


def test_each_channel_stamps_its_own_prompt_on_a_copy():
    base = {"launch_root": "process"}
    responses = prepare_agent_config(
        base,
        cwd="/work",
        instructions_path=responses_special_instructions_path(),
    )
    completions = prepare_agent_config(
        base,
        cwd="/work",
        instructions_path=completions_special_instructions_path(),
    )
    assert "chat_completions_special_instructions_path" not in base
    assert base["launch_root"] == "process"
    responses_text = Path(
        responses["chat_completions_special_instructions_path"]
    ).read_text(encoding="utf-8")
    completions_text = Path(
        completions["chat_completions_special_instructions_path"]
    ).read_text(encoding="utf-8")
    assert "prose" in responses_text
    assert "Do not call a tool to ask" in responses_text
    assert "<think>" in completions_text
    assert "final prose" in completions_text
    assert (
        responses["chat_completions_special_instructions_path"]
        != completions["chat_completions_special_instructions_path"]
    )


def test_ask_human_stop_does_not_require_prior_list():
    config = {}
    apply_chat_runtime_defaults(config)
    assert config["stop_tools"] == [
        ASK_HUMAN_TOOL_NAME,
        INTERMEDIATE_REPLY_TOOL_NAME,
    ]


def test_harbor_and_local_backend():
    assert backend_rejection({}) is None
    assert backend_rejection({"os_backend": "local"}) is None
    text = backend_rejection({"os_backend": "harbor"})
    assert text is not None
    assert "harbor" in text
    rejected = backend_rejection({"os_backend": "docker"})
    assert rejected is not None
    assert "docker" in rejected
