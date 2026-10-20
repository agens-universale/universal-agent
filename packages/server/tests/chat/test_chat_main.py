"""Старт процесса: настройки из командной строки, как у gc-cli / gc-acp."""

import pytest
from gc_server.chat.main import _parse_args, main
from gc_testkit.settings import make_settings


def test_parse_args_keeps_own_flags_and_collects_setting_paths():
    bare = _parse_args([])
    assert bare.config is None
    assert bare.profile is None
    assert bare.overrides == {}

    known = _parse_args(["--config", "c.yml", "--profile", "work"])
    assert known.config == "c.yml"
    assert known.profile == "work"
    assert known.overrides == {}

    mixed = _parse_args(
        ["--config", "c.yml", "--level", "max", "--agent.verbose"]
    )
    assert mixed.config == "c.yml"
    assert mixed.overrides == {"level": "max", "agent.verbose": True}


def test_parse_args_coerces_setting_values():
    args = _parse_args(
        ["--level", "max", "--agent.max_steps=50", "--agent.verbose", "true"]
    )
    assert args.overrides == {
        "level": "max",
        "agent.max_steps": 50,
        "agent.verbose": True,
    }


def test_flat_name_is_an_error_with_path_hint(capsys):
    with pytest.raises(SystemExit) as exc:
        _parse_args(["--max_steps", "50"])
    assert exc.value.code == 2
    assert "--agent.max_steps" in capsys.readouterr().err


def _start(monkeypatch, tmp_path, argv, base=None):
    held = {}

    def load(path, profile_override=None):
        held["profile"] = profile_override
        return make_settings(
            {
                "os_backend": "local",
                "paths.threads_data_path": str(tmp_path),
                **(base or {}),
            }
        )

    async def serve(loaded):
        held["loaded"] = loaded

    monkeypatch.setattr("gc_server.chat.main.load_settings", load)
    monkeypatch.setattr("gc_server.chat.main.serve", serve)
    return main(argv), held


def test_setting_overrides_apply_after_profile(monkeypatch, tmp_path):
    code, held = _start(
        monkeypatch,
        tmp_path,
        [
            "--profile",
            "work",
            "--level",
            "max",
            "--agent.max_steps",
            "7",
            "--model",
            "forced",
            "--chat_api.port",
            "9001",
        ],
        {"agent.max_steps": 100, "models": {"flash": {"model": "m"}}},
    )
    assert code == 0
    loaded = held["loaded"]
    assert held["profile"] == "work"
    assert loaded.settings.level == "max"
    assert loaded.settings.agent.max_steps == 7
    assert loaded.settings.agent.model == "forced"
    assert loaded.settings.chat_api.port == 9001
    assert loaded.provenance.source("agent.model").layer == "cli"
    assert any("легаси-форс" in n for n in loaded.runtime.notices)


def test_unknown_setting_path_fails(monkeypatch, tmp_path, capsys):
    code, held = _start(monkeypatch, tmp_path, ["--chat_api.prot", "9001"])
    assert code == 2 and "loaded" not in held
    assert "неизвестная настройка --chat_api.prot" in capsys.readouterr().err


def test_profile_and_cli_do_not_break_chat_contract(monkeypatch, tmp_path):
    """Профиль с TUI-судьёй и finish-gate, а также те же ключи из CLI не
    меняют контракт gc-chat: канал читает только ``chat_api.*`` — проза
    allow, судья off, напоминания todo 0."""
    code, held = _start(
        monkeypatch,
        tmp_path,
        [
            "--profile",
            "work",
            "--tui.text_answer_mode",
            "deny",
            "--agent.strict_turn_finish_validation",
            "true",
        ],
        {
            "tui.text_answer_mode": "judge",
            "tui.finish_gate_mode": "on",
            "agent.strict_turn_finish_validation": True,
            "todo.reminder_steps": 5,
        },
    )
    assert code == 0
    loaded = held["loaded"]
    assert loaded.runtime.text_answer_mode == "allow"
    assert loaded.settings.agent.strict_turn_finish_validation is False
    assert loaded.runtime.finish_gate_smart is False
    assert loaded.settings.todo.reminder_steps == 0
