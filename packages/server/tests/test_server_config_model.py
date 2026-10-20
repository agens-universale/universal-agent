"""Модель config_server.yml: умолчания, наследование, старые ключи, слои агента."""

import pytest
import yaml
from gc_server.config import (
    ServerConfigError,
    load_server_config,
    surface_settings,
)
from gc_testkit.settings import make_settings


def _write(tmp_path, doc: dict):
    path = tmp_path / "config_server.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return str(path)


def test_defaults_without_file_sections(tmp_path):
    cfg = load_server_config(_write(tmp_path, {}))
    assert cfg.server.apis == ["webhook"]
    assert cfg.address("webhook") == ("127.0.0.1", 8000)
    assert cfg.address("chat") == ("127.0.0.1", 8090)
    assert cfg.address("a2a") == ("127.0.0.1", 8091)
    assert cfg.address("swarm") == ("127.0.0.1", 8092)
    assert cfg.webhook.default_system == "generic"
    assert cfg.webhook.context_build_wait_seconds == 120
    assert cfg.webhook.threads_cleanup.enabled is False
    assert cfg.config_path.endswith("config_server.yml")


def test_host_and_token_are_inherited_and_overridable(tmp_path):
    cfg = load_server_config(
        _write(
            tmp_path,
            {
                "server": {"host": "10.0.0.5", "auth_token": "common"},
                "a2a": {"host": "0.0.0.0", "auth_token": "own"},
            },
        )
    )
    assert cfg.surface("chat").host == "10.0.0.5"
    assert cfg.surface("chat").auth_token == "common"
    assert cfg.surface("a2a").host == "0.0.0.0"
    assert cfg.surface("a2a").auth_token == "own"


@pytest.mark.parametrize(
    ("doc", "new_place"),
    [
        ({"defaults": {"system": "x"}}, "webhook.default_system"),
        (
            {"agent": {"server_agentmd_path": "./a.md"}},
            "webhook.agent_md_path",
        ),
        (
            {"server": {"context_build_wait_seconds": 5}},
            "webhook.context_build_wait_seconds",
        ),
        ({"threads_cleanup": {"enabled": True}}, "webhook.threads_cleanup"),
        ({"swarm": {"enabled": True}}, "server.apis"),
        ({"logging": {"save_logs": False}}, "server.logging"),
    ],
)
def test_old_keys_name_their_new_place(tmp_path, doc, new_place):
    with pytest.raises(ServerConfigError) as err:
        load_server_config(_write(tmp_path, doc))
    assert new_place in str(err.value)


def test_unknown_key_is_an_error(tmp_path):
    with pytest.raises(ServerConfigError) as err:
        load_server_config(_write(tmp_path, {"chat": {"colour": "red"}}))
    assert "colour" in str(err.value)


def test_unknown_surface_in_apis_is_an_error(tmp_path):
    with pytest.raises(ServerConfigError):
        load_server_config(_write(tmp_path, {"server": {"apis": ["ftp"]}}))


def test_swarm_keeps_its_own_keys(tmp_path):
    cfg = load_server_config(
        _write(
            tmp_path,
            {"swarm": {"max_concurrent": 3, "limits": {"memory_mb": 512}}},
        )
    )
    raw = cfg.swarm_section()
    assert raw["swarm"]["max_concurrent"] == 3
    assert raw["swarm"]["limits"] == {"memory_mb": 512}
    assert raw["config_path"] == cfg.config_path


def test_env_placeholders_are_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("GC_TEST_SERVER_TOKEN", "s3cret")
    cfg = load_server_config(
        _write(
            tmp_path,
            {"server": {"auth_token": "${ENV:GC_TEST_SERVER_TOKEN}"}},
        )
    )
    assert cfg.surface("webhook").auth_token == "s3cret"


def test_agent_overrides_layer_common_then_surface(tmp_path):
    cfg = load_server_config(
        _write(
            tmp_path,
            {
                "agent_overrides": {
                    "agent.max_steps": 11,
                    "agent.verbose": True,
                },
                "chat": {"agent_overrides": {"agent.max_steps": 22}},
            },
        )
    )
    base = make_settings({})
    chat = surface_settings(base, cfg, "chat")
    webhook = surface_settings(base, cfg, "webhook")
    assert chat.settings.agent.max_steps == 22
    assert chat.settings.agent.verbose is True
    assert webhook.settings.agent.max_steps == 11


def test_unknown_override_names_its_surface(tmp_path):
    cfg = load_server_config(
        _write(tmp_path, {"chat": {"agent_overrides": {"max_steps": 3}}})
    )
    with pytest.raises(ServerConfigError) as err:
        surface_settings(make_settings({}), cfg, "chat")
    assert "chat.agent_overrides" in str(err.value)


def test_agent_md_path_is_resolved_next_to_the_file(tmp_path):
    (tmp_path / "role.md").write_text("роль", encoding="utf-8")
    cfg = load_server_config(
        _write(tmp_path, {"webhook": {"agent_md_path": "./role.md"}})
    )
    loaded = surface_settings(make_settings({}), cfg, "webhook")
    assert loaded.settings.paths.agent_md_path == str(tmp_path / "role.md")


def test_webhook_role_defaults_to_the_bundled_server_prompt(tmp_path):
    cfg = load_server_config(_write(tmp_path, {}))
    loaded = surface_settings(make_settings({}), cfg, "webhook")
    assert loaded.settings.paths.agent_md_path.endswith(
        "prompts/server_agent.md"
    )
    chat = surface_settings(make_settings({}), cfg, "chat")
    assert chat.settings.paths.agent_md_path in (None, "")


def test_explicit_missing_file_is_an_error(tmp_path):
    """Явно названный файл (флаг, SERVER_CONFIG_PATH) не нашёлся — отказ:
    опечатка в пути не должна поднимать сервер на умолчаниях без токена."""
    with pytest.raises(ServerConfigError) as err:
        load_server_config(str(tmp_path / "nope.yml"))
    assert "nope.yml" in str(err.value)


def test_no_file_anywhere_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SERVER_CONFIG_PATH", raising=False)
    monkeypatch.setattr("gc_server.config.repo_root", lambda: None)
    monkeypatch.setattr(
        "gc_server.config.bundled_root", lambda: tmp_path / "assets"
    )
    cfg = load_server_config(None)
    assert cfg.config_path == ""
    assert cfg.server.apis == ["webhook"]
