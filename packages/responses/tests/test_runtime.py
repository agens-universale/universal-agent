"""Канальные дефолты: проза allow, finish-gate off, наджи todo выключены."""

from pathlib import Path

import pytest
from gc_core.config_modules.responses import ResponsesConfig
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
)
from gc_responses.main import (
    apply_responses_runtime_defaults,
    backend_rejection,
)
from gc_responses.prompt_assets import responses_special_instructions_path


def test_defaults_allow_and_finish_gate_off():
    config = {
        "strict_turn_finish_validation": True,
        "text_answer_mode": "deny",
        "finish_gate_smart": True,
        "todo_params": {"reminder_steps": 4, "other": 1},
    }
    apply_responses_runtime_defaults(config)
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
        "responses_text_answer_mode": "judge",
        "responses_finish_gate_mode": "smart",
        "responses_todo_reminder_steps": 3,
        "strict_turn_finish_validation": False,
    }
    apply_responses_runtime_defaults(config)
    assert config["text_answer_mode"] == "judge"
    assert config["strict_turn_finish_validation"] is True
    assert config["finish_gate_smart"] is True
    assert config["todo_params"]["reminder_steps"] == 3


def test_finish_gate_on_overrides_agent_false():
    config = {
        "responses_finish_gate_mode": "on",
        "strict_turn_finish_validation": False,
        "todo_params": {"reminder_steps": 2},
    }
    apply_responses_runtime_defaults(config)
    assert config["strict_turn_finish_validation"] is True
    assert config["finish_gate_smart"] is False
    assert config["todo_params"]["reminder_steps"] == 0


def test_yaml_section_forbids_unknown_and_flattens(tmp_path):
    with pytest.raises(Exception):
        ResponsesConfig(todo_nudge=1)
    cfg = ResponsesConfig(
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
        "responses:\n"
        "  text_answer_mode: allow\n"
        '  finish_gate_mode: "off"\n'
        "  todo_reminder_steps: 0\n",
        encoding="utf-8",
    )
    loaded = cfgmod.load_config(str(path))
    assert loaded["responses_todo_reminder_steps"] == 0
    assert loaded["strict_turn_finish_validation"] is True
    apply_responses_runtime_defaults(loaded)
    assert loaded["text_answer_mode"] == "allow"
    assert loaded["strict_turn_finish_validation"] is False
    assert loaded["finish_gate_smart"] is False
    assert loaded["todo_params"]["reminder_steps"] == 0
    assert loaded["responses_host"] == "127.0.0.1"
    assert loaded["responses_port"] == 8090


def test_ask_human_is_stopped_and_role_prompt_is_set():
    config = {
        "stop_tools": [
            "exec_command",
            ASK_HUMAN_TOOL_NAME,
            INTERMEDIATE_REPLY_TOOL_NAME,
        ]
    }
    apply_responses_runtime_defaults(config)
    assert config["stop_tools"].count(ASK_HUMAN_TOOL_NAME) == 1
    assert config["stop_tools"].count(INTERMEDIATE_REPLY_TOOL_NAME) == 1
    assert config["stop_tools"][0] == "exec_command"
    path = config["chat_completions_special_instructions_path"]
    assert path == responses_special_instructions_path()
    text = Path(path).read_text(encoding="utf-8")
    assert "turn_ask_human" in text
    assert "prose" in text


def test_ask_human_stop_does_not_require_prior_list():
    config = {}
    apply_responses_runtime_defaults(config)
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
