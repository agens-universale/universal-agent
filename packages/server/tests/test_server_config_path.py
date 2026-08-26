import pytest

from gc_core.config import cli_forces_legacy_model
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_core.model_router import ModelRouter
from gc_server.rest_api import server_config
from gc_server.rest_api.server_config import load_server_config


def test_server_config_path_from_env_vars(monkeypatch, tmp_path):
    custom = tmp_path / "my_server.yml"
    custom.write_text(
        "server:\n  host: 0.0.0.0\n  port: 9999\nagent:\n  server_agentmd_path: a.md\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=custom))
    cfg = load_server_config()
    assert cfg["server"].get("port") == 9999


def test_server_config_path_none_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=None))
    monkeypatch.setattr("gc_server.rest_api.server_config.PROJECT_ROOT", str(tmp_path))
    cfg = load_server_config()
    assert cfg["server"]["host"] == "0.0.0.0"
    assert cfg["server"]["port"] == 8000


# --- Task 10 (ревью): agent_overrides идут через apply_cli_overrides ------------
#
# Контракт (CLAUDE.md): agent_overrides перекрывают config.yml так же, как
# CLI-аргументы -> agent_overrides.model/completions_url поверх непустого
# merged-реестра моделей обязан включать легаси-форс, как --model.


def test_agent_overrides_model_with_registry_forces_legacy(monkeypatch):
    monkeypatch.setattr(
        server_config,
        "load_config",
        lambda: {
            "models": {"flash": {}},
            "slots": {"default": "flash"},
            "model": "flash",
            "completions_url": "http://127.0.0.1:8014/v1/chat/completions",
        },
    )

    cfg = server_config.build_agent_config_for_server(
        {"agent_overrides": {"model": "forced-model"}}
    )

    assert cli_forces_legacy_model(cfg) is True
    router = ModelRouter(cfg, level_provider=lambda: "default", force_legacy=True)
    resolved = router.resolve("main")
    assert resolved.alias is None
    assert resolved.model == "forced-model"
    assert resolved.source == "легаси-форс --model"


def test_agent_overrides_without_model_is_not_forced(monkeypatch):
    monkeypatch.setattr(
        server_config,
        "load_config",
        lambda: {"models": {"flash": {}}, "slots": {"default": "flash"}},
    )

    cfg = server_config.build_agent_config_for_server(
        {"agent_overrides": {"stop_tools": ["write_to_file"]}}
    )

    assert cli_forces_legacy_model(cfg) is False


def test_agent_overrides_other_keys_still_applied(monkeypatch):
    """Регресс: прочие agent_overrides (например, stop_tools) применяются как раньше."""
    monkeypatch.setattr(server_config, "load_config", lambda: {"model": "m"})

    cfg = server_config.build_agent_config_for_server(
        {"agent_overrides": {"stop_tools": ["write_to_file"], "max_steps": 7}}
    )

    assert cfg["stop_tools"] == ["write_to_file"]
    assert cfg["max_steps"] == 7
