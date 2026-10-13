"""Старт процесса: секция a2a, как у gc-chat своя секция."""

import pytest
from gc_a2a.main import (
    _parse_args,
    apply_a2a_runtime_defaults,
    backend_rejection,
    main,
)
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
)
from gc_testkit.settings import make_settings


def test_parse_args_keeps_own_flags_and_collects_setting_paths():
    bare = _parse_args([])
    assert bare.config is None
    assert bare.profile is None
    assert bare.overrides == {}
    mixed = _parse_args(
        ["--config", "c.yml", "--level", "max", "--a2a.port", "9001"]
    )
    assert mixed.config == "c.yml"
    assert mixed.overrides["level"] == "max"
    assert mixed.overrides["a2a.port"] == 9001


def test_flat_name_is_an_error_with_path_hint(capsys):
    with pytest.raises(SystemExit) as exc:
        _parse_args(["--max_steps", "50"])
    assert exc.value.code == 2
    assert "--agent.max_steps" in capsys.readouterr().err


def test_defaults_allow_prose_and_keep_ask_human():
    base = make_settings(
        **{
            "agent.strict_turn_finish_validation": True,
            "agent.max_steps": 7,
            "todo.reminder_steps": 4,
            "text_answer_mode": "deny",
            "finish_gate_smart": True,
        }
    )
    loaded = apply_a2a_runtime_defaults(base)
    assert loaded.runtime.text_answer_mode == "allow"
    assert loaded.settings.agent.strict_turn_finish_validation is False
    assert loaded.runtime.finish_gate_smart is False
    assert loaded.settings.todo.reminder_steps == 0
    assert loaded.settings.agent.max_steps == 7
    assert ASK_HUMAN_TOOL_NAME not in loaded.settings.agent.stop_tools
    assert INTERMEDIATE_REPLY_TOOL_NAME in loaded.settings.agent.stop_tools
    assert base.settings.agent.stop_tools == []
    assert base.runtime.text_answer_mode == "deny"


def test_channel_keys_win():
    loaded = apply_a2a_runtime_defaults(
        make_settings(
            **{
                "a2a.text_answer_mode": "judge",
                "a2a.finish_gate_mode": "smart",
                "a2a.todo_reminder_steps": 3,
            }
        )
    )
    assert loaded.runtime.text_answer_mode == "judge"
    assert loaded.settings.agent.strict_turn_finish_validation is True
    assert loaded.runtime.finish_gate_smart is True
    assert loaded.settings.todo.reminder_steps == 3


def test_yaml_section_loads(tmp_path):
    from gc_core.config import load_settings

    path = tmp_path / "config.yml"
    path.write_text(
        "a2a:\n"
        "  host: 127.0.0.1\n"
        "  port: 9001\n"
        "  name: Пир\n"
        '  description: "Свой агент"\n'
        "  text_answer_mode: allow\n"
        '  finish_gate_mode: "off"\n'
        "  todo_reminder_steps: 0\n",
        encoding="utf-8",
    )
    loaded = load_settings(str(path))
    assert loaded.settings.a2a.port == 9001
    assert loaded.settings.a2a.name == "Пир"
    assert loaded.settings.a2a.description == "Свой агент"
    loaded = apply_a2a_runtime_defaults(loaded)
    assert loaded.runtime.text_answer_mode == "allow"
    assert loaded.settings.agent.strict_turn_finish_validation is False


def test_harbor_exits_before_bind(monkeypatch, capsys):
    def load(path, profile_override=None):
        del path, profile_override
        return make_settings(os_backend="harbor")

    monkeypatch.setattr("gc_a2a.main.load_settings", load)
    code = main([])
    assert code == 1
    assert "harbor" in capsys.readouterr().err


def test_harbor_rejection_text():
    text = backend_rejection(make_settings(os_backend="harbor"))
    assert text is not None
    assert "gc-a2a" in text
    assert "harbor" in text
    assert backend_rejection(make_settings(os_backend="local")) is None
