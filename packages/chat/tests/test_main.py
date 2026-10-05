"""Старт процесса: CLI-оверрайды конфига, как у gc-cli / gc-acp."""

from gc_chat.main import _parse_args, main


def test_parse_args_keeps_own_flags_and_collects_overrides():
    bare = _parse_args([])
    assert bare.config is None
    assert bare.profile is None
    assert bare.overrides == {}

    known = _parse_args(["--config", "c.yml", "--profile", "work"])
    assert known.config == "c.yml"
    assert known.profile == "work"
    assert known.overrides == {}

    mixed = _parse_args(["--config", "c.yml", "--level", "max", "--verbose"])
    assert mixed.config == "c.yml"
    assert mixed.overrides == {"level": "max", "verbose": True}

    junk = _parse_args(["nope", "--foo", "bar"])
    assert junk.config is None
    assert junk.overrides == {"foo": "bar"}


def test_parse_args_coerces_override_types():
    args = _parse_args(
        ["--level", "max", "--max_steps=50", "--verbose", "true"]
    )
    assert args.overrides == {
        "level": "max",
        "max_steps": 50,
        "verbose": True,
    }


def test_cli_overrides_apply_after_profile(monkeypatch, tmp_path):
    held = {}

    def load(path, profile_override=None):
        held["profile"] = profile_override
        return {
            "os_backend": "local",
            "threads_data_path": str(tmp_path),
            "level": "default",
            "max_steps": 100,
            "models": {"flash": {"model": "m"}},
            "chat_api_port": 8090,
        }, None

    async def serve(config):
        held["config"] = config

    monkeypatch.setattr("gc_chat.main.load_config_ex", load)
    monkeypatch.setattr("gc_chat.main.serve", serve)
    assert (
        main(
            [
                "--profile",
                "work",
                "--level",
                "max",
                "--max_steps",
                "7",
                "--model",
                "forced",
                "--chat_api_port",
                "9001",
            ]
        )
        == 0
    )
    cfg = held["config"]
    assert held["profile"] == "work"
    assert cfg["level"] == "max"
    assert cfg["max_steps"] == 7
    assert cfg["model"] == "forced"
    assert cfg["chat_api_port"] == 9001
    assert "model" in cfg["cli_explicit_keys"]
    assert any("легаси-форс" in n for n in cfg["compat_notices"])


def test_profile_tui_judge_does_not_break_chat_contract(monkeypatch, tmp_path):
    """Профиль с TUI-судьёй и finish-gate не ломает контракт gc-chat.

    Канал читает только ``chat_api_*``; ``tui_*``, плоский
    ``text_answer_mode`` и ``agent.strict_turn_finish_validation`` из
    профиля/CLI перекрываются дефолтами процесса: проза allow, судья off.
    """
    held = {}

    def load(path, profile_override=None):
        held["profile"] = profile_override
        return {
            "os_backend": "local",
            "threads_data_path": str(tmp_path),
            # как после профиля work / TUI:
            "tui_text_answer_mode": "judge",
            "tui_finish_gate_mode": "on",
            "text_answer_mode": "deny",
            "strict_turn_finish_validation": True,
            "finish_gate_smart": True,
            "todo_params": {"reminder_steps": 5},
        }, None

    async def serve(config):
        held["config"] = config

    monkeypatch.setattr("gc_chat.main.load_config_ex", load)
    monkeypatch.setattr("gc_chat.main.serve", serve)
    assert (
        main(
            [
                "--profile",
                "work",
                "--text_answer_mode",
                "deny",
                "--strict_turn_finish_validation",
                "true",
            ]
        )
        == 0
    )
    cfg = held["config"]
    assert held["profile"] == "work"
    assert cfg["text_answer_mode"] == "allow"
    assert cfg["strict_turn_finish_validation"] is False
    assert cfg["finish_gate_smart"] is False
    assert cfg["todo_params"]["reminder_steps"] == 0
