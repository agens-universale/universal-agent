"""Канальные дефолты: проза allow, finish-gate off, наджи todo выключены."""

from pathlib import Path

import pytest
from gc_completions.main import (
    apply_completions_runtime_defaults,
    backend_rejection,
)
from gc_completions.prompt_assets import completions_special_instructions_path
from gc_core.config_modules.completions import CompletionsConfig
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
    apply_completions_runtime_defaults(config)
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
        "completions_text_answer_mode": "judge",
        "completions_finish_gate_mode": "smart",
        "completions_todo_reminder_steps": 3,
        "strict_turn_finish_validation": False,
    }
    apply_completions_runtime_defaults(config)
    assert config["text_answer_mode"] == "judge"
    assert config["strict_turn_finish_validation"] is True
    assert config["finish_gate_smart"] is True
    assert config["todo_params"]["reminder_steps"] == 3


def test_yaml_section_forbids_unknown_and_flattens(tmp_path):
    with pytest.raises(Exception):
        CompletionsConfig(todo_nudge=1)
    cfg = CompletionsConfig(
        todo_reminder_steps=5,
        text_answer_mode="deny",
        finish_gate_mode="off",
        cors_origins=["http://ui", "*"],
    )
    assert cfg.todo_reminder_steps == 5
    assert cfg.cors_origins == ["http://ui"]
    from gc_core import config as cfgmod

    path = tmp_path / "config.yml"
    path.write_text(
        "agent:\n"
        "  strict_turn_finish_validation: true\n"
        "completions:\n"
        "  text_answer_mode: allow\n"
        '  finish_gate_mode: "off"\n'
        "  todo_reminder_steps: 0\n",
        encoding="utf-8",
    )
    loaded = cfgmod.load_config(str(path))
    assert loaded["completions_todo_reminder_steps"] == 0
    assert loaded["strict_turn_finish_validation"] is True
    apply_completions_runtime_defaults(loaded)
    assert loaded["text_answer_mode"] == "allow"
    assert loaded["strict_turn_finish_validation"] is False
    assert loaded["completions_host"] == "127.0.0.1"
    assert loaded["completions_port"] == 8091


def test_role_prompt_says_the_trace_is_not_replayed():
    config: dict = {}
    apply_completions_runtime_defaults(config)
    path = config["chat_completions_special_instructions_path"]
    assert path == completions_special_instructions_path()
    text = Path(path).read_text(encoding="utf-8")
    assert "<think>" in text
    assert "final prose" in text


def test_harbor_and_local_backend():
    assert backend_rejection({}) is None
    assert backend_rejection({"os_backend": "local"}) is None
    text = backend_rejection({"os_backend": "harbor"})
    assert text is not None
    assert "harbor" in text
