"""Старт chat через gc-serve: настройки из командной строки, как у gc-cli."""

import pytest
from gc_server import cli
from gc_testkit.settings import make_settings


def test_parse_args_keeps_own_flags_and_collects_setting_paths():
    bare = cli._parse_args([])
    assert bare.config is None
    assert bare.profile is None
    assert bare.server_config is None
    assert bare.surfaces == []
    assert bare.overrides == {}

    known = cli._parse_args(["chat", "--config", "c.yml", "--profile", "work"])
    assert known.surfaces == ["chat"]
    assert known.config == "c.yml"
    assert known.profile == "work"
    assert known.overrides == {}

    mixed = cli._parse_args(
        ["chat", "--config", "c.yml", "--model", "max", "--agent.verbose"]
    )
    assert mixed.overrides == {"model": "max", "agent.verbose": True}


def test_parse_args_coerces_setting_values():
    args = cli._parse_args(
        ["--model", "max", "--agent.max_steps=50", "--agent.verbose", "true"]
    )
    assert args.overrides == {
        "model": "max",
        "agent.max_steps": 50,
        "agent.verbose": True,
    }


def test_flat_name_is_an_error_with_path_hint(capsys):
    with pytest.raises(SystemExit) as exc:
        cli._parse_args(["--max_steps", "50"])
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

    async def serve(runtime, surfaces, **kwargs):
        held["surfaces"] = surfaces

    server_config = tmp_path / "config_server.yml"
    server_config.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("gc_server.cli.load_settings", load)
    monkeypatch.setattr("gc_server.cli.aserve", serve)
    code = cli.main(["chat", "--server-config", str(server_config), *argv])
    return code, held


def test_setting_overrides_apply_after_profile(monkeypatch, tmp_path):
    code, held = _start(
        monkeypatch,
        tmp_path,
        [
            "--profile",
            "work",
            "--model",
            "forced",
            "--agent.max_steps",
            "7",
        ],
        {"agent.max_steps": 100, "models": {"flash": {"model": "m"}}},
    )
    assert code == 0
    loaded = held["surfaces"][0].settings
    assert held["profile"] == "work"
    assert loaded.settings.agent.max_steps == 7
    assert loaded.settings.agent.model == "forced"
    assert loaded.provenance.source("agent.model").layer == "cli"
    assert any("легаси-форс" in n for n in loaded.runtime.notices)


def test_unknown_setting_path_fails(monkeypatch, tmp_path, capsys):
    code, held = _start(monkeypatch, tmp_path, ["--agent.prot", "9001"])
    assert code == 2 and "surfaces" not in held
    assert "неизвестная настройка --agent.prot" in capsys.readouterr().err


def test_profile_and_cli_do_not_break_chat_contract(monkeypatch, tmp_path):
    """Профиль с TUI-судьёй и finish-gate, а также те же ключи из CLI не
    меняют контракт chat: канал читает только свою секцию — проза allow,
    судья off, напоминания todo 0."""
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
    loaded = held["surfaces"][0].settings
    assert loaded.runtime.text_answer_mode == "allow"
    assert loaded.settings.agent.strict_turn_finish_validation is False
    assert loaded.runtime.finish_gate_smart is False
    assert loaded.settings.todo.reminder_steps == 0


def _start_model(monkeypatch, tmp_path, value):
    return _start(
        monkeypatch,
        tmp_path,
        ["--model", value],
        {
            "models": {"flash": {"model": "m"}, "pro": {"model": "p"}},
            "slots": {"default": "flash"},
        },
    )


def _assert_choice(monkeypatch, tmp_path, value):
    code, held = _start_model(monkeypatch, tmp_path, value)
    assert code == 0
    loaded = held["surfaces"][0].settings
    assert loaded.settings.model == value
    assert not any("легаси-форс" in n for n in loaded.runtime.notices)


def test_model_flag_auto_is_a_choice(monkeypatch, tmp_path):
    _assert_choice(monkeypatch, tmp_path, "auto")


def test_model_flag_alias_is_a_choice(monkeypatch, tmp_path):
    _assert_choice(monkeypatch, tmp_path, "pro")


def test_model_flag_level_word_is_a_choice(monkeypatch, tmp_path):
    _assert_choice(monkeypatch, tmp_path, "max")


def test_model_flag_raw_id_forces_legacy(monkeypatch, tmp_path):
    code, held = _start_model(monkeypatch, tmp_path, "raw-id")
    assert code == 0
    loaded = held["surfaces"][0].settings
    assert loaded.settings.agent.model == "raw-id"
    assert any("легаси-форс" in n for n in loaded.runtime.notices)


def test_level_flag_errors_with_model_hint(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        _start(monkeypatch, tmp_path, ["--level", "max"])
    assert exc.value.code == 2
    assert "--model" in capsys.readouterr().err
