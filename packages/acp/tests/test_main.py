"""Старт процесса: конфиг, harbor, тишина в stdout, ACP runtime defaults."""

import json

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


def test_parse_args_keeps_own_flags_and_ignores_the_rest():
    bare = _parse_args([])
    assert bare.config is None
    assert bare.profile is None

    jetbrains = _parse_args(
        ["acp", "--experimental", "--config", "c.yml", "--profile", "work"]
    )
    assert jetbrains.config == "c.yml"
    assert jetbrains.profile == "work"

    vscode = _parse_args(["--profile", "acp"])
    assert vscode.profile == "acp"
    assert vscode.config is None

    junk = _parse_args(["nope", "--foo", "bar", "acp"])
    assert junk.config is None
    assert junk.profile is None


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
    assert main(["acp", "--config", "c.yml", "--profile", "work"]) == 0
    assert held == {
        "path": "c.yml",
        "profile": "work",
        "started": True,
        "text_answer_mode": "allow",
    }
