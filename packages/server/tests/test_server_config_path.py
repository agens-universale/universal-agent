"""Где ищется config_server.yml, что попадает в лог, как правки агента ложатся
поверх config.yml и где ищется роль агента поверхности."""

import logging
import os

import pytest
from gc_core.bundled_assets import bundled_root
from gc_core.config import cli_forces_legacy_model
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_core.model_router import ModelRouter
from gc_server import config as server_config
from gc_server.config import (
    ServerConfig,
    ServerConfigError,
    load_server_config,
    surface_settings,
)
from gc_testkit.settings import make_settings


def test_server_config_path_from_env_vars(monkeypatch, tmp_path):
    custom = tmp_path / "my_server.yml"
    custom.write_text("webhook:\n  port: 9999\n", encoding="utf-8")
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=custom))
    cfg = load_server_config()
    assert cfg.address("webhook")[1] == 9999
    assert cfg.config_path == str(custom)


def test_explicit_flag_wins_over_env(monkeypatch, tmp_path):
    env_file = tmp_path / "env.yml"
    env_file.write_text("webhook:\n  port: 1111\n", encoding="utf-8")
    flag_file = tmp_path / "flag.yml"
    flag_file.write_text("webhook:\n  port: 2222\n", encoding="utf-8")
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=env_file))
    assert load_server_config(str(flag_file)).address("webhook")[1] == 2222


def test_nothing_found_falls_back_to_defaults(monkeypatch, tmp_path):
    """Файла нет нигде — умолчания и пустой путь: «якорь неизвестен», а не
    «якорь = ассеты»."""
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=None))
    monkeypatch.setattr(server_config, "bundled_root", lambda: tmp_path / "a")
    monkeypatch.setattr(server_config, "repo_root", lambda: None)
    monkeypatch.chdir(tmp_path)
    cfg = load_server_config()
    assert cfg.config_path == ""
    assert cfg.address("webhook") == ("127.0.0.1", 8000)


def test_load_server_config_logs_which_candidate_won(
    monkeypatch, tmp_path, caplog
):
    """Какой именно config_server.yml прочитан — обязано быть в логе: список
    кандидатов начинается с каталога запуска, и молчаливый промах резолва
    однажды поднял сервер без токена."""
    (tmp_path / "config_server.yml").write_text(
        "webhook:\n  port: 7777\n", encoding="utf-8"
    )
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=None))
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.INFO, logger=server_config.__name__):
        cfg = load_server_config()
    assert cfg.address("webhook")[1] == 7777
    assert os.path.realpath(str(tmp_path / "config_server.yml")) in (
        os.path.realpath(cfg.config_path)
    )
    assert "config_server.yml" in caplog.text


# --- agent_overrides — тем же путём, что флаги CLI -------------------------


def _with(cfg: dict, base: dict, name: str = "webhook"):
    return surface_settings(
        make_settings(base), ServerConfig.model_validate(cfg), name
    )


def test_agent_overrides_model_with_registry_forces_legacy():
    loaded = _with(
        {"agent_overrides": {"agent.model": "forced-model"}},
        {
            "models": {"flash": {}},
            "slots": {"default": "flash"},
            "agent.model": "flash",
            "backend.completions_url": "http://127.0.0.1:8014/v1/chat/completions",
        },
    )
    # agent_overrides видны легаси-форсу как заданные из CLI.
    assert loaded.provenance.source("agent.model").layer == "cli"
    assert cli_forces_legacy_model(loaded) is True
    router = ModelRouter(
        loaded, choice_provider=lambda: "default", force_legacy=True
    )
    resolved = router.resolve("main")
    assert resolved.alias is None
    assert resolved.model == "forced-model"
    assert resolved.source == "легаси-форс --model"


def test_agent_overrides_without_model_is_not_forced():
    loaded = _with(
        {"agent_overrides": {"agent.stop_tools": ["write_to_file"]}},
        {"models": {"flash": {}}, "slots": {"default": "flash"}},
    )
    assert cli_forces_legacy_model(loaded) is False


def test_agent_overrides_other_keys_still_applied():
    settings = _with(
        {
            "agent_overrides": {
                "agent.stop_tools": ["write_to_file"],
                "agent.max_steps": 7,
            }
        },
        {"agent.model": "m"},
    ).settings
    assert settings.agent.stop_tools == ["write_to_file"]
    assert settings.agent.max_steps == 7


def test_agent_overrides_section_overrides_only_its_fields():
    """Вложенная секция перекрывает только заданные поля; прочие значения
    config.yml остаются."""
    settings = _with(
        {"agent_overrides": {"agent": {"stop_tools": ["write_to_file"]}}},
        {"agent.model": "m", "agent.max_steps": 3},
    ).settings
    assert settings.agent.stop_tools == ["write_to_file"]
    assert settings.agent.max_steps == 3
    assert settings.agent.model == "m"


def test_agent_overrides_flat_name_fails_with_path_hint():
    """Плоских имён нет: сервер не стартует, сообщение подсказывает путь."""
    with pytest.raises(ServerConfigError) as exc:
        _with({"agent_overrides": {"max_steps": 7}}, {})
    message = str(exc.value)
    assert message.startswith(
        "agent_overrides: неизвестная настройка max_steps"
    )
    assert "agent.max_steps" in message
    assert "--" not in message


# --- роль агента поверхности ------------------------------------------------


def _agent_md(value: str, cfg_path):
    cfg = ServerConfig.model_validate(
        {"webhook": {"agent_md_path": value}}
    ).model_copy(update={"config_path": str(cfg_path)})
    return surface_settings(
        make_settings({}), cfg, "webhook"
    ).settings.paths.agent_md_path


def test_relative_agent_md_is_found_next_to_the_config(tmp_path):
    """Файл рядом с config_server.yml обязан находиться: иначе агент молча
    работал бы без системных инструкций."""
    own = tmp_path / "server_agent.md"
    own.write_text("# инструкции\n", encoding="utf-8")
    assert _agent_md(
        "./server_agent.md", tmp_path / "config_server.yml"
    ) == str(own)


def test_relative_agent_md_falls_back_to_bundled_assets(tmp_path):
    resolved = _agent_md(
        "prompts/server_agent.md", tmp_path / "config_server.yml"
    )
    assert os.path.isfile(resolved), resolved
    assert resolved.startswith(str(bundled_root()))


def test_missing_agent_md_is_loud(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=server_config.__name__):
        _agent_md("./nowhere.md", tmp_path / "config_server.yml")
    assert "webhook.agent_md_path" in caplog.text
    assert str(tmp_path) in caplog.text


def test_absolute_agent_md_is_taken_as_is(tmp_path):
    explicit = tmp_path / "elsewhere" / "a.md"
    explicit.parent.mkdir(parents=True, exist_ok=True)
    explicit.write_text("x", encoding="utf-8")
    assert _agent_md(str(explicit), tmp_path / "config_server.yml") == str(
        explicit
    )
