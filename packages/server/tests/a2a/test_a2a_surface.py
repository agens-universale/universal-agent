"""Поверхность a2a: канальные умолчания, отказы до старта, предупреждения."""

import pytest
import yaml
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
)
from gc_server import cli
from gc_server.a2a.surface import (
    A2aSurface,
    apply_a2a_runtime_defaults,
    auth_rejection,
)
from gc_server.app import GRACEFUL_SHUTDOWN_SECONDS, ServerRuntime
from gc_server.chat.surface import backend_rejection
from gc_server.config import (
    A2aConfig,
    ServerConfig,
    ServerConfigError,
    load_server_config,
)
from gc_testkit.settings import make_settings


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
        make_settings(),
        A2aConfig(
            text_answer_mode="judge",
            finish_gate_mode="smart",
            todo_reminder_steps=3,
        ),
    )
    assert loaded.runtime.text_answer_mode == "judge"
    assert loaded.settings.agent.strict_turn_finish_validation is True
    assert loaded.runtime.finish_gate_smart is True
    assert loaded.settings.todo.reminder_steps == 3


def test_section_comes_from_config_server(tmp_path):
    path = tmp_path / "config_server.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "a2a": {
                    "port": 9001,
                    "name": "Пир",
                    "description": "Свой агент",
                    "finish_gate_mode": "off",
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    cfg = load_server_config(str(path))
    assert cfg.address("a2a") == ("127.0.0.1", 9001)
    assert cfg.a2a.name == "Пир"
    assert cfg.a2a.description == "Свой агент"


def test_harbor_is_refused_before_bind():
    text = backend_rejection(make_settings(os_backend="harbor"), "a2a")
    assert text is not None and "a2a" in text and "harbor" in text
    assert backend_rejection(make_settings(os_backend="local"), "a2a") is None
    runtime = ServerRuntime(
        base=make_settings(os_backend="harbor"), config=ServerConfig()
    )
    with pytest.raises(ServerConfigError) as err:
        A2aSurface(runtime)
    assert "harbor" in str(err.value)


def test_auth_rejection_blocks_non_loopback_without_token():
    assert auth_rejection(A2aConfig(host="127.0.0.1")) is None
    assert auth_rejection(A2aConfig(host="::1")) is None
    blocked = auth_rejection(A2aConfig(host="0.0.0.0"))
    assert blocked is not None and "auth_token" in blocked
    assert auth_rejection(A2aConfig(host="0.0.0.0", auth_token="s")) is None


def test_graceful_shutdown_is_bounded():
    assert 0 < GRACEFUL_SHUTDOWN_SECONDS < 60


def test_non_loopback_without_token_exits_before_bind(tmp_path, capsys):
    path = tmp_path / "config_server.yml"
    path.write_text("a2a:\n  host: 0.0.0.0\n", encoding="utf-8")
    code = cli.main(["a2a", "--server-config", str(path)])
    assert code == 1
    assert "auth_token" in capsys.readouterr().err


def test_a2a_declares_no_human_and_external_words():
    """Пир — не человек: подтверждение спросить некого, а его слова для
    судьи действий — запрос внешней системы."""
    loaded = apply_a2a_runtime_defaults(make_settings())
    assert loaded.runtime.human_channel is False
    assert loaded.runtime.external_user_words is True


def _surface(mode: str, tmp_path) -> A2aSurface:
    runtime = ServerRuntime(
        base=make_settings(
            {
                "security.permissions.default_mode": mode,
                "paths.threads_data_path": str(tmp_path),
            }
        ),
        config=ServerConfig(),
    )
    return A2aSurface(runtime)


def test_plan_mode_is_warned_about(capsys, tmp_path):
    cli._warn(ServerConfig(), [_surface("plan", tmp_path)])
    err = capsys.readouterr().err
    assert "a2a: режим разрешений по умолчанию — plan" in err
    assert "security.permissions.default_mode: edit" in err


def test_edit_mode_has_no_mode_warning(capsys, tmp_path):
    cli._warn(ServerConfig(), [_surface("edit", tmp_path)])
    assert "режим разрешений" not in capsys.readouterr().err
