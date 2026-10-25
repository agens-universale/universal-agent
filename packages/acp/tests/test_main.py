"""Старт процесса: конфиг, harbor, тишина в stdout, ACP runtime defaults."""

import json

import pytest
from gc_acp.main import (
    _parse_args,
    apply_acp_runtime_defaults,
    backend_rejection,
    main,
)
from gc_acp.prompt_assets import acp_special_instructions_path
from gc_acp.protocol import StdoutWriter
from gc_testkit.settings import make_settings


def _assert_acp_defaults(loaded):
    assert loaded.runtime.text_answer_mode == "allow"
    assert loaded.settings.agent.strict_turn_finish_validation is False
    assert loaded.runtime.finish_gate_smart is False
    assert loaded.settings.todo.reminder_steps == 0


def test_apply_acp_runtime_defaults_allow_gate_off_todo_off():
    loaded = apply_acp_runtime_defaults(make_settings())
    _assert_acp_defaults(loaded)
    assert (
        loaded.settings.paths.chat_completions_special_instructions_path
        == (acp_special_instructions_path())
    )
    assert loaded.settings.agent.stop_tools == [
        "turn_ask_human",
        "turn_intermediate_reply",
    ]


def test_apply_acp_runtime_defaults_respects_overrides():
    loaded = apply_acp_runtime_defaults(
        make_settings(
            {
                "acp.text_answer_mode": "judge",
                "acp.finish_gate_mode": "smart",
                "acp.todo_reminder_steps": 7,
            }
        )
    )
    assert loaded.runtime.text_answer_mode == "judge"
    assert loaded.settings.agent.strict_turn_finish_validation is True
    assert loaded.runtime.finish_gate_smart is True
    assert loaded.settings.todo.reminder_steps == 7


def test_apply_acp_runtime_defaults_keeps_existing_stop_tools():
    loaded = apply_acp_runtime_defaults(
        make_settings(
            {"agent.stop_tools": ["project_memory", "turn_ask_human"]}
        )
    )
    assert loaded.settings.agent.stop_tools == [
        "project_memory",
        "turn_ask_human",
        "turn_intermediate_reply",
    ]


def test_acp_special_instructions_asset_exists():
    path = acp_special_instructions_path()
    with open(path, encoding="utf-8") as fd:
        text = fd.read()
    assert "Discussion vs implementation" in text
    assert "don't write code" in text.lower() or "Do not write" in text
    assert "Do not call a tool to ask" in text


def test_backend_rejection_names_harbor_and_accepts_local():
    assert backend_rejection(make_settings({"os_backend": "local"})) is None
    assert backend_rejection(make_settings()) is None
    text = backend_rejection(make_settings({"os_backend": "harbor"}))
    assert text is not None
    assert "harbor" in text


def test_harbor_exits_before_protocol(monkeypatch, capsys):
    seen = {}

    def load(path, profile_override=None):
        seen["path"] = path
        seen["profile"] = profile_override
        return make_settings({"os_backend": "harbor"})

    def run(coro):
        coro.close()
        raise AssertionError("protocol started")

    monkeypatch.setattr("gc_acp.main.load_settings", load)
    monkeypatch.setattr("gc_acp.main.asyncio.run", run)
    assert main(["--config", "c.yml", "--profile", "work"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "harbor" in captured.err
    assert seen == {"path": "c.yml", "profile": "work"}


def test_unreadable_config_exits_before_protocol(monkeypatch, capsys):
    def load(path, profile_override=None):
        raise ValueError("сломан")

    def run(coro):
        coro.close()
        raise AssertionError("protocol started")

    monkeypatch.setattr("gc_acp.main.load_settings", load)
    monkeypatch.setattr(
        "gc_acp.main.explain_broken_config", lambda *args, **kwargs: "сломан"
    )
    monkeypatch.setattr("gc_acp.main.asyncio.run", run)
    assert main(["--config", "missing.yml"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "сломан" in captured.err


async def _no_lines():
    if False:
        yield ""


def test_local_config_is_silent_on_stdout(monkeypatch, capsys, tmp_path):
    def load(path, profile_override=None):
        return make_settings(
            {"os_backend": "local", "paths.threads_data_path": str(tmp_path)}
        )

    monkeypatch.setattr("gc_acp.main.load_settings", load)
    monkeypatch.setattr("gc_acp.main.stdin_lines", _no_lines)
    assert main(["--config", "c.yml"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_local_start_applies_runtime_defaults(monkeypatch, tmp_path):
    held = {}

    def load(path, profile_override=None):
        return make_settings(
            {"os_backend": "local", "paths.threads_data_path": str(tmp_path)}
        )

    async def serve(config, protocol_out):
        held["config"] = config
        held["protocol_out"] = protocol_out

    monkeypatch.setattr("gc_acp.main.load_settings", load)
    monkeypatch.setattr("gc_acp.main.serve", serve)
    assert main(["--config", "c.yml"]) == 0
    loaded = held["config"]
    _assert_acp_defaults(loaded)
    assert loaded.settings.logging.console_trace is False
    assert loaded.settings.paths.chat_completions_special_instructions_path.endswith(
        "chat_completions_acp_special_instructions.md"
    )
    assert "turn_ask_human" in loaded.settings.agent.stop_tools
    assert "turn_intermediate_reply" in loaded.settings.agent.stop_tools


def test_core_print_stays_off_protocol_stdout(monkeypatch, capsys, tmp_path):
    def load(path, profile_override=None):
        return make_settings(
            {"os_backend": "local", "paths.threads_data_path": str(tmp_path)}
        )

    async def serve(_config, protocol_out):
        print("[MCP] demo: недоступен — connection refused (пропущен)")
        await StdoutWriter(protocol_out).send(
            {"jsonrpc": "2.0", "id": 1, "result": {}}
        )

    monkeypatch.setattr("gc_acp.main.load_settings", load)
    monkeypatch.setattr("gc_acp.main.serve", serve)
    assert main(["--config", "c.yml"]) == 0
    print("after")
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert json.loads(lines[0]) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert lines[-1] == "after"
    assert "[MCP]" in captured.err
    assert "[MCP]" not in captured.out


def test_unsupported_backend_is_rejected():
    text = backend_rejection(make_settings({"os_backend": "docker"}))
    assert text is not None
    assert "docker" in text


def test_parse_args_keeps_own_flags_and_collects_setting_paths():
    bare = _parse_args([])
    assert bare.config is None
    assert bare.profile is None
    assert bare.overrides == {} and bare.foreign_flags == []

    jetbrains = _parse_args(
        ["acp", "--experimental", "--config", "c.yml", "--profile", "work"]
    )
    assert jetbrains.config == "c.yml"
    assert jetbrains.profile == "work"
    # Флаг клиента без точки — не настройка: пропускается.
    assert jetbrains.overrides == {}
    assert jetbrains.foreign_flags == ["experimental"]

    vscode = _parse_args(["--profile", "acp"])
    assert vscode.profile == "acp"
    assert vscode.config is None

    junk = _parse_args(["nope", "--foo", "bar", "acp"])
    assert junk.config is None
    assert junk.profile is None
    assert junk.overrides == {} and junk.foreign_flags == ["foo"]


def test_parse_args_coerces_setting_values():
    args = _parse_args(
        [
            "--model",
            "max",
            "--agent.max_steps=50",
            "--agent.verbose",
            "true",
        ]
    )
    assert args.overrides == {
        "model": "max",
        "agent.max_steps": 50,
        "agent.verbose": True,
    }


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

    async def serve(loaded, protocol_out):
        held["loaded"] = loaded

    monkeypatch.setattr("gc_acp.main.load_settings", load)
    monkeypatch.setattr("gc_acp.main.serve", serve)
    return main(argv), held


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
    loaded = held["loaded"]
    assert held["profile"] == "work"
    assert loaded.settings.agent.max_steps == 7
    assert loaded.settings.agent.model == "forced"
    assert loaded.provenance.source("agent.model").layer == "cli"
    assert any("легаси-форс" in n for n in loaded.runtime.notices)


def test_unknown_setting_path_fails(monkeypatch, tmp_path, capsys):
    code, held = _start(monkeypatch, tmp_path, ["--agent.max_step", "7"])
    assert code == 2 and "loaded" not in held
    err = capsys.readouterr().err
    assert "неизвестная настройка --agent.max_step" in err


def test_flat_name_is_skipped_with_path_hint(monkeypatch, tmp_path, capsys):
    code, held = _start(monkeypatch, tmp_path, ["--max_steps", "7"])
    assert code == 0
    assert held["loaded"].settings.agent.max_steps != 7
    assert "--max_steps пропущен" in capsys.readouterr().err


def test_invalid_setting_value_is_rejected_without_value(
    monkeypatch, tmp_path, capsys
):
    code, _ = _start(
        monkeypatch,
        tmp_path,
        ["--backend.chat_completion_headers", "Bearer sk-secret"],
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "backend.chat_completion_headers" in err
    assert "sk-secret" not in err


def test_jetbrains_acp_arg_starts_protocol(monkeypatch, tmp_path):
    held = {}

    def load(path, profile_override=None):
        held["path"] = path
        held["profile"] = profile_override
        return make_settings(
            {"os_backend": "local", "paths.threads_data_path": str(tmp_path)}
        )

    async def serve(config, protocol_out):
        held["started"] = True
        held["text_answer_mode"] = config.runtime.text_answer_mode

    monkeypatch.setattr("gc_acp.main.load_settings", load)
    monkeypatch.setattr("gc_acp.main.serve", serve)
    assert (
        main(
            ["acp", "--experimental", "--config", "c.yml", "--profile", "work"]
        )
        == 0
    )
    assert held == {
        "path": "c.yml",
        "profile": "work",
        "started": True,
        "text_answer_mode": "allow",
    }


def test_abbreviated_own_flags_fail_instead_of_default_config(capsys):
    """`--conf x.yml` / `--prof acp` — не флаги клиента: пропуск запустил бы
    агента на конфиге по умолчанию. Чужие флаги клиента — по-прежнему мимо."""
    assert main(["--conf", "x.yml"]) == 2
    assert "--conf (--config)" in capsys.readouterr().err
    assert main(["--prof", "acp"]) == 2
    assert "--prof (--profile)" in capsys.readouterr().err
    assert _parse_args(["acp", "--experimental"]).abbreviated == []
    # Короткие флаги клиента, похожие на начало нашего, — его собственные.
    for flag in ("--c", "--mod", "--lev", "--pro", "--comp"):
        assert _parse_args(["acp", flag]).abbreviated == [], flag


def test_acp_declares_a_human_whose_words_are_his_own():
    """Вопросы подтверждения в ACP задаёт человек в IDE; его слова — свои."""
    loaded = apply_acp_runtime_defaults(make_settings())
    assert loaded.runtime.human_channel is True
    assert loaded.runtime.external_user_words is False


def test_parse_args_accepts_auto_model():
    assert _parse_args(["--model", "auto"]).overrides == {"model": "auto"}


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
    loaded = held["loaded"]
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
    loaded = held["loaded"]
    assert loaded.settings.agent.model == "raw-id"
    assert any("легаси-форс" in n for n in loaded.runtime.notices)


def test_level_flag_errors_with_model_hint(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        _start(monkeypatch, tmp_path, ["--level", "max"])
    assert exc.value.code == 2
    assert "--model" in capsys.readouterr().err
