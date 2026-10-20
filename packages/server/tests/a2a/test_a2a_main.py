"""Старт процесса: секция a2a, как у gc-chat своя секция."""

import pytest
from gc_server.a2a.main import (
    _parse_args,
    apply_a2a_runtime_defaults,
    auth_rejection,
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

    monkeypatch.setattr("gc_server.a2a.main.load_settings", load)
    code = main([])
    assert code == 1
    assert "harbor" in capsys.readouterr().err


def test_harbor_rejection_text():
    text = backend_rejection(make_settings(os_backend="harbor"))
    assert text is not None
    assert "gc-a2a" in text
    assert "harbor" in text
    assert backend_rejection(make_settings(os_backend="local")) is None


def test_auth_rejection_blocks_non_loopback_without_token():
    assert auth_rejection(make_settings()) is None
    assert auth_rejection(make_settings(**{"a2a.host": "127.0.0.1"})) is None
    assert auth_rejection(make_settings(**{"a2a.host": "::1"})) is None
    blocked = auth_rejection(make_settings(**{"a2a.host": "0.0.0.0"}))
    assert blocked is not None
    assert "auth_token" in blocked
    assert (
        auth_rejection(
            make_settings(
                **{"a2a.host": "0.0.0.0", "a2a.auth_token": "secret"}
            )
        )
        is None
    )


async def test_serve_bounds_graceful_shutdown(tmp_path, monkeypatch):
    captured = {}

    class FakeServer:
        def __init__(self, config) -> None:
            captured["timeout"] = config.timeout_graceful_shutdown
            self.should_exit = False

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("gc_server.a2a.main.uvicorn.Server", FakeServer)
    monkeypatch.setattr(
        "gc_server.a2a.main._log_routes", lambda *_a, **_k: None
    )
    from gc_server.a2a.main import serve

    await serve(make_settings(**{"paths.threads_data_path": str(tmp_path)}))
    assert captured["timeout"] not in (None, 0)


def test_non_loopback_without_token_exits_before_bind(monkeypatch, capsys):
    def load(path, profile_override=None):
        del path, profile_override
        return make_settings(**{"a2a.host": "0.0.0.0"})

    monkeypatch.setattr("gc_server.a2a.main.load_settings", load)
    code = main([])
    assert code == 1
    assert "auth_token" in capsys.readouterr().err


def test_a2a_declares_no_human_and_external_words():
    """Пир — не человек: подтверждение спросить некого, а его слова для
    судьи действий — запрос внешней системы (спека §7.1, §3.7)."""
    loaded = apply_a2a_runtime_defaults(make_settings())
    assert loaded.runtime.human_channel is False
    assert loaded.runtime.external_user_words is True


def _start(monkeypatch, mode):
    served = []

    def load(path, profile_override=None):
        del path, profile_override
        return make_settings({"security.permissions.default_mode": mode})

    async def fake_serve(loaded):
        served.append(loaded)

    monkeypatch.setattr("gc_server.a2a.main.load_settings", load)
    monkeypatch.setattr("gc_server.a2a.main.serve", fake_serve)
    assert main([]) == 0
    assert served  # старт не прерван
    return served


def test_plan_mode_is_warned_about_and_the_server_still_starts(
    monkeypatch, capsys
):
    _start(monkeypatch, "plan")
    err = capsys.readouterr().err
    assert "gc-a2a: режим разрешений по умолчанию — plan" in err
    assert "--security.permissions.default_mode edit" in err


def test_edit_mode_starts_without_the_warning(monkeypatch, capsys):
    _start(monkeypatch, "edit")
    assert "режим разрешений" not in capsys.readouterr().err
