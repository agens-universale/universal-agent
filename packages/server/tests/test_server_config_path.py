import logging
import os

import pytest

from gc_core.config import cli_forces_legacy_model
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_core.model_router import ModelRouter
from gc_server.rest_api import server_config
from gc_server.rest_api.server_config import load_server_config
from gc_testkit.settings import make_settings


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
    monkeypatch.setattr(
        "gc_server.rest_api.server_config.PROJECT_ROOT", str(tmp_path)
    )
    cfg = load_server_config()
    assert cfg["server"]["host"] == "0.0.0.0"
    assert cfg["server"]["port"] == 8000


def test_load_server_config_logs_which_candidate_won(
    monkeypatch, tmp_path, caplog
):
    """Какой именно config_server.yml прочитан — обязано быть в логе.

    Список кандидатов начинается с CWD, и до этой строки узнать победителя было
    неоткуда: молчаливый промах резолва однажды поднял сервер с auth_token=None.
    """
    (tmp_path / "config_server.yml").write_text(
        "server:\n  port: 7777\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=None))
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(
        logging.INFO, logger="gc_server.rest_api.server_config"
    ):
        cfg = load_server_config()

    assert cfg["server"]["port"] == 7777
    assert os.path.realpath(str(tmp_path / "config_server.yml")) in caplog.text


def test_load_server_config_reports_the_file_it_read(monkeypatch, tmp_path):
    """Прочитанный путь обязан уезжать вместе с конфигом.

    Он единственный источник каталога, от которого резолвятся относительные
    пути этого же файла (swarm.skills_dir и соседи). Пока его не было, swarm
    резолвил их от корня bundled-ассетов — то есть внутрь чужого пакета.
    """
    custom = tmp_path / "my_server.yml"
    custom.write_text("server:\n  port: 9999\n", encoding="utf-8")
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=custom))

    assert load_server_config()["config_path"] == str(custom)


def test_load_server_config_reports_empty_path_when_nothing_found(
    monkeypatch, tmp_path
):
    """Файла нет — ключ есть, но пустой: «якорь неизвестен», а не «якорь = ассеты»."""
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=None))
    monkeypatch.setattr(server_config, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(server_config, "repo_root", lambda: None)
    monkeypatch.chdir(tmp_path)

    assert load_server_config()["config_path"] == ""


# --- Task 10 (ревью): agent_overrides идут через apply_cli_overrides ------------
#
# Контракт (CLAUDE.md): agent_overrides перекрывают config.yml так же, как
# CLI-аргументы -> agent_overrides.model/completions_url поверх непустого
# merged-реестра моделей обязан включать легаси-форс, как --model.


def test_agent_overrides_model_with_registry_forces_legacy(monkeypatch):
    monkeypatch.setattr(
        server_config,
        "load_settings",
        lambda: make_settings(
            {
                "models": {"flash": {}},
                "slots": {"default": "flash"},
                "agent.model": "flash",
                "backend.completions_url": "http://127.0.0.1:8014/v1/chat/completions",
            }
        ),
    )

    loaded = server_config.build_agent_settings_for_server(
        {"agent_overrides": {"agent.model": "forced-model"}}
    )

    # agent_overrides видны легаси-форсу как заданные из CLI.
    assert loaded.provenance.source("agent.model").layer == "cli"
    assert cli_forces_legacy_model(loaded) is True
    router = ModelRouter(
        loaded, level_provider=lambda: "default", force_legacy=True
    )
    resolved = router.resolve("main")
    assert resolved.alias is None
    assert resolved.model == "forced-model"
    assert resolved.source == "легаси-форс --model"


def test_agent_overrides_without_model_is_not_forced(monkeypatch):
    monkeypatch.setattr(
        server_config,
        "load_settings",
        lambda: make_settings(
            {"models": {"flash": {}}, "slots": {"default": "flash"}}
        ),
    )

    loaded = server_config.build_agent_settings_for_server(
        {"agent_overrides": {"agent.stop_tools": ["write_to_file"]}}
    )

    assert cli_forces_legacy_model(loaded) is False


def test_agent_overrides_other_keys_still_applied(monkeypatch):
    """Регресс: прочие agent_overrides (например, stop_tools) применяются как раньше."""
    monkeypatch.setattr(
        server_config,
        "load_settings",
        lambda: make_settings({"agent.model": "m"}),
    )

    settings = server_config.build_agent_settings_for_server(
        {
            "agent_overrides": {
                "agent.stop_tools": ["write_to_file"],
                "agent.max_steps": 7,
            }
        }
    ).settings

    assert settings.agent.stop_tools == ["write_to_file"]
    assert settings.agent.max_steps == 7


def test_agent_overrides_section_overrides_only_its_fields(monkeypatch):
    """Вложенная секция перекрывает только заданные поля — как deep-merge у
    swarm; прочие значения config.yml остаются."""
    monkeypatch.setattr(
        server_config,
        "load_settings",
        lambda: make_settings({"agent.model": "m", "agent.max_steps": 3}),
    )

    settings = server_config.build_agent_settings_for_server(
        {"agent_overrides": {"agent": {"stop_tools": ["write_to_file"]}}}
    ).settings

    assert settings.agent.stop_tools == ["write_to_file"]
    assert settings.agent.max_steps == 3
    assert settings.agent.model == "m"


def test_agent_overrides_flat_name_fails_with_path_hint(monkeypatch):
    """Плоских имён нет: сервер не стартует, сообщение подсказывает путь."""
    monkeypatch.setattr(server_config, "load_settings", make_settings)

    with pytest.raises(ValueError) as exc:
        server_config.build_agent_settings_for_server(
            {"agent_overrides": {"max_steps": 7}}
        )
    message = str(exc.value)
    assert message.startswith(
        "agent_overrides: неизвестная настройка max_steps"
    )
    assert "agent.max_steps" in message
    assert "--" not in message


def _agent_md(server_agentmd_path, cfg_path):
    """``paths.agent_md_path`` для заданной пары «значение поля, путь конфига»."""
    return server_config.build_agent_settings_for_server(
        {
            "config_path": str(cfg_path),
            "agent": {"server_agentmd_path": server_agentmd_path},
        }
    ).settings.paths.agent_md_path


def test_relative_agent_md_is_found_next_to_the_config(tmp_path):
    """Пример из docs/server.md — файл рядом с config_server.yml — обязан находиться.

    На develop `PROJECT_ROOT` был корнем репозитория, где лежит и сам
    `config_server.yml`, поэтому `server_agentmd_path: "./server_agent.md"`
    указывал на соседний файл. После переезда ассетов внутрь пакета тот же
    якорь стал вести в `packages/core/assets`, и пример перестал работать —
    молча: `build_instructions` возвращает пустой текст, а сервер продолжает
    обслуживать запросы агентом БЕЗ системных инструкций.
    """
    own = tmp_path / "server_agent.md"
    own.write_text("# инструкции\n", encoding="utf-8")
    assert _agent_md(
        "./server_agent.md", tmp_path / "config_server.yml"
    ) == str(own)


def test_relative_agent_md_falls_back_to_bundled_assets(tmp_path):
    """Рядом с конфигом нет — берём bundled-промпт дистрибутива."""
    resolved = _agent_md(
        "prompts/server_agent.md", tmp_path / "config_server.yml"
    )
    assert os.path.isfile(resolved), resolved
    assert resolved.startswith(server_config.PROJECT_ROOT)


def test_missing_agent_md_is_loud(tmp_path, caplog):
    """Ненайденный файл кричит: тихо работать без инструкций сервер не должен."""
    with caplog.at_level(logging.WARNING, logger=server_config.__name__):
        _agent_md("./nowhere.md", tmp_path / "config_server.yml")
    assert "server_agentmd_path" in caplog.text
    assert str(tmp_path) in caplog.text


def test_absolute_agent_md_is_taken_as_is(tmp_path):
    explicit = tmp_path / "elsewhere" / "a.md"
    explicit.parent.mkdir(parents=True, exist_ok=True)
    explicit.write_text("x", encoding="utf-8")
    assert _agent_md(str(explicit), tmp_path / "config_server.yml") == str(
        explicit
    )
