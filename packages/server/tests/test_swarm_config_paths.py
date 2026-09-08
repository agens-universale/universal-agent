import os
from pathlib import Path

import pytest
import yaml

from gc_core.bundled_assets import repo_root
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars, project_slug
from gc_server.rest_api.server_config import load_server_config
from gc_server.swarm import config as swarm_config
from gc_server.swarm.config import build_swarm_config


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    fake = EnvVars(gc_home=tmp_path)
    monkeypatch.setattr(env_vars, "env", fake)
    monkeypatch.chdir(tmp_path)
    return fake


def test_swarm_threads_default_under_state_projects_slug(isolated_env, tmp_path):
    cfg = build_swarm_config({})
    threads_dir = Path(cfg["threads_dir"])
    assert threads_dir == tmp_path / "projects" / project_slug() / "swarm" / "threads"


def test_swarm_sandbox_default_under_state_projects_slug(isolated_env, tmp_path):
    cfg = build_swarm_config({})
    sandbox_dir = Path(cfg["sandbox_root_dir"])
    assert sandbox_dir == tmp_path / "projects" / project_slug() / "swarm" / "sandboxes"


def test_swarm_explicit_threads_dir_used_as_is(isolated_env, tmp_path):
    explicit = tmp_path / "custom_threads"
    cfg = build_swarm_config({"swarm": {"threads_dir": str(explicit)}})
    assert Path(cfg["threads_dir"]) == explicit


def test_default_wrapper_path_exists_and_is_executable(isolated_env):
    """Дефолтная обёртка едет в колесе как package data — путь обязан быть живым.

    Пустое значение wrapper_path — единственная конфигурация, которая работает
    из коробки; если она перестанет резолвиться, /swarm/run отвалится на первом
    же запросе, как это уже было с "scripts/run-cli-in-sandbox.sh".
    """
    cfg = build_swarm_config({})
    wrapper = cfg["wrapper_path"]
    assert os.path.isfile(wrapper), wrapper
    assert os.access(wrapper, os.X_OK), wrapper


def test_demo_template_swarm_paths_are_usable():
    """Шаблон demo_config_server.yml должен подниматься как есть.

    Он не покрывался ничем: после переезда обёртки внутрь пакета его
    "scripts/run-cli-in-sandbox.sh" резолвился в gc_server/swarm/scripts/, файла
    там нет, и первый же POST /swarm/run падал FileNotFoundError из Popen.
    """
    root = repo_root()
    assert root is not None, "тест обязан идти в рабочей копии, а не скипаться"
    raw = yaml.safe_load((root / "demo_config_server.yml").read_text(encoding="utf-8"))
    cfg = build_swarm_config(raw)

    assert os.path.isfile(cfg["wrapper_path"]), cfg["wrapper_path"]
    assert os.access(cfg["wrapper_path"], os.X_OK), cfg["wrapper_path"]
    # agentmd_path из того же шаблона резолвится от корня ассетов — проверяем
    # заодно, чтобы следующий переезд не оставил и его указывать в пустоту.
    assert os.path.isfile(cfg["agentmd_path"]), cfg["agentmd_path"]


def test_swarm_wrapper_and_agentmd_paths_unchanged(isolated_env, tmp_path):
    """Пакетные ресурсы остаются с PROJECT_ROOT-дефолтами.

    skills_dir из этого списка ушёл намеренно: он не файл дистрибутива, а
    пользовательский контент, и его дефолт переехал под gc_paths() — см.
    test_skills_dir_default_lives_under_gc_data.
    """
    cfg = build_swarm_config({})
    assert tmp_path not in Path(cfg["wrapper_path"]).parents
    assert tmp_path not in Path(cfg["agentmd_path"]).parents


# ---------------------------------------------------------------------------
# Якорь относительных путей пользовательского конфига
#
# До разбиения на пакеты `bundled_root()` возвращал корень репозитория — то есть
# ровно тот каталог, где лежит config_server.yml, и "./agent_logs" давал каталог
# рядом с конфигом. После переезда ассетов внутрь пакета тот же якорь стал
# указывать в site-packages/gc_core/assets: логи агентов писались в чужой пакет и
# исчезали при `uv tool upgrade`, а несуществующий <assets>/swarm_skills молча
# оставлял песочницу без скиллов.
# ---------------------------------------------------------------------------


def _server_cfg_with_config_at(path: Path, swarm: dict) -> dict:
    """SERVER_CONFIG в том виде, в каком его отдаёт load_server_config()."""
    return {"config_path": str(path), "swarm": swarm}


def test_relative_user_paths_resolve_next_to_server_config(isolated_env, tmp_path):
    """Относительные пути берутся от каталога САМОГО config_server.yml."""
    conf_dir = tmp_path / "etc"
    conf_dir.mkdir()
    cfg = build_swarm_config(
        _server_cfg_with_config_at(
            conf_dir / "config_server.yml",
            {
                "swarm_threads_dir": "swarm_threads",
                "sandbox_root_dir": ".swarm_sandboxes",
                "swarm_logs_dir": "./agent_logs",
                "skills_dir": "./swarm_skills",
            },
        )
    )
    assert Path(cfg["threads_dir"]) == conf_dir / "swarm_threads"
    assert Path(cfg["sandbox_root_dir"]) == conf_dir / ".swarm_sandboxes"
    assert Path(cfg["swarm_logs_dir"]) == conf_dir / "agent_logs"
    assert Path(cfg["skills_dir"]) == conf_dir / "swarm_skills"


def test_relative_user_paths_never_land_in_bundled_assets(isolated_env, tmp_path):
    """Сама регрессия: ни один пользовательский путь не уезжает в каталог ассетов.

    Проверяем не «равно ожидаемому», а «не внутри чужого пакета» — так сетка
    переживёт следующий переезд ассетов и всё равно поймает возврат якоря.
    """
    conf_dir = tmp_path / "etc"
    conf_dir.mkdir()
    assets = Path(swarm_config.PROJECT_ROOT).resolve()
    cfg = build_swarm_config(
        _server_cfg_with_config_at(
            conf_dir / "config_server.yml",
            {
                "swarm_threads_dir": "swarm_threads",
                "sandbox_root_dir": ".swarm_sandboxes",
                "swarm_logs_dir": "./agent_logs",
                "skills_dir": "./swarm_skills",
            },
        )
    )
    for key in ("threads_dir", "sandbox_root_dir", "swarm_logs_dir", "skills_dir"):
        resolved = Path(cfg[key]).resolve()
        assert assets not in resolved.parents and resolved != assets, (
            f"{key} снова резолвится внутрь ассетов: {resolved}"
        )


def test_absolute_user_paths_are_left_alone(isolated_env, tmp_path):
    """Абсолютный путь пользователя не трогаем — каталог конфига ни при чём."""
    conf_dir = tmp_path / "etc"
    conf_dir.mkdir()
    cfg = build_swarm_config(
        _server_cfg_with_config_at(
            conf_dir / "config_server.yml",
            {"swarm_logs_dir": "/var/lib/gc/agent_logs"},
        )
    )
    assert cfg["swarm_logs_dir"] == "/var/lib/gc/agent_logs"


def test_explicit_config_dir_beats_config_path(isolated_env, tmp_path):
    """Явный аргумент сильнее ключа config_path — для вызывающих без dict'а."""
    conf_dir = tmp_path / "etc"
    conf_dir.mkdir()
    explicit = tmp_path / "elsewhere"
    cfg = build_swarm_config(
        _server_cfg_with_config_at(
            conf_dir / "config_server.yml", {"skills_dir": "./swarm_skills"}
        ),
        config_dir=str(explicit),
    )
    assert Path(cfg["skills_dir"]) == explicit / "swarm_skills"


def test_without_config_path_relative_resolves_from_cwd(isolated_env, tmp_path):
    """Конфига нет вовсе (минимальный дефолт) — якорь CWD, первый кандидат поиска."""
    cfg = build_swarm_config({"swarm": {"skills_dir": "./swarm_skills"}})
    assert Path(cfg["skills_dir"]) == tmp_path / "swarm_skills"


def test_skills_dir_default_lives_under_gc_data(isolated_env, tmp_path):
    """Дефолт скиллов — в XDG-каталоге данных, а не в каталоге ассетов.

    Каталог ассетов принадлежит чужому пакету: `uv tool upgrade` его
    переустанавливает, а до переезда его вообще не существовало — runner тихо
    пропускал копирование.
    """
    cfg = build_swarm_config({})
    assert Path(cfg["skills_dir"]) == env_vars.gc_paths()["data"] / "swarm_skills"


def test_config_path_of_load_server_config_feeds_the_anchor(monkeypatch, tmp_path):
    """Сквозной путь: load_server_config → build_swarm_config, без ручной склейки.

    Именно так их зовёт gc_server.server, и именно здесь ломался якорь: путь
    прочитанного файла раньше не доезжал до swarm-конфига вовсе.
    """
    conf_dir = tmp_path / "etc"
    conf_dir.mkdir()
    (conf_dir / "config_server.yml").write_text(
        "swarm:\n  enabled: true\n  swarm_logs_dir: ./agent_logs\n", encoding="utf-8",
    )
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=conf_dir / "config_server.yml"))

    cfg = build_swarm_config(load_server_config())
    assert Path(cfg["swarm_logs_dir"]) == conf_dir / "agent_logs"


# ---------------------------------------------------------------------------
# agent_overrides propagation (унификация webhook + swarm)
# ---------------------------------------------------------------------------


def test_swarm_propagates_agent_overrides_from_server_cfg(isolated_env):
    """server_cfg.agent_overrides должен попасть в swarm_cfg как есть.

    Materializer потом авто-обернёт flat-формат в {agent: ...}.
    """
    server_cfg = {
        "agent_overrides": {
            "stop_tools": ["write_to_file"],
            "max_steps": 100,
        },
    }
    cfg = build_swarm_config(server_cfg)
    assert cfg["agent_overrides"] == {
        "stop_tools": ["write_to_file"],
        "max_steps": 100,
    }


def test_swarm_propagates_agent_overrides_raw_form(isolated_env):
    """Raw-формат overrides (с секциями agent: / backend: ) пробрасывается без изменений."""
    server_cfg = {
        "agent_overrides": {
            "agent": {"max_steps": 999},
            "backend": {"api_mode": "responses"},
        },
    }
    cfg = build_swarm_config(server_cfg)
    assert cfg["agent_overrides"]["agent"]["max_steps"] == 999
    assert cfg["agent_overrides"]["backend"]["api_mode"] == "responses"


def test_swarm_agent_overrides_empty_when_missing(isolated_env):
    """Если в server_cfg нет agent_overrides — в swarm_cfg ключ есть, но пустой."""
    cfg = build_swarm_config({})
    assert cfg["agent_overrides"] == {}


def test_swarm_agent_overrides_invalid_type_falls_back_to_empty(isolated_env):
    """agent_overrides не-dict (list, str) — fallback на пустой dict, без падения."""
    cfg = build_swarm_config({"agent_overrides": ["not", "a", "dict"]})
    assert cfg["agent_overrides"] == {}

    cfg = build_swarm_config({"agent_overrides": "stop_tools"})
    assert cfg["agent_overrides"] == {}


def test_swarm_agent_overrides_is_a_copy_not_reference(isolated_env):
    """build_swarm_config должен сделать копию, чтобы мутации swarm_cfg
    не затрагивали оригинал в server_cfg."""
    original = {"stop_tools": ["write_to_file"]}
    server_cfg = {"agent_overrides": original}
    cfg = build_swarm_config(server_cfg)
    cfg["agent_overrides"]["max_steps"] = 100
    assert "max_steps" not in original


def test_wrapper_path_from_develop_template_falls_back_to_the_packaged_one(
    isolated_env, tmp_path, caplog
):
    """Прежнее штатное значение develop не роняет sworm, а даёт обёртку из пакета.

    `demo_config_server.yml` на develop раскладывал `wrapper_path:
    "scripts/run-cli-in-sandbox.sh"`, и это работало: `bundled_root()` был корнем
    репозитория. Обёртка с тех пор переехала внутрь пакета, в `scripts/` она не
    попадала ни в одно колесо. У всех, кто скопировал шаблон, относительный путь
    указывал в несуществующий `gc_server/swarm/scripts/...`, и КАЖДЫЙ
    `POST /swarm/run` отвечал 500 `FileNotFoundError` — при живой обёртке рядом.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger=swarm_config.__name__):
        cfg = build_swarm_config(
            {
                "config_path": str(tmp_path / "config_server.yml"),
                "swarm": {"enabled": True,
                          "wrapper_path": "scripts/run-cli-in-sandbox.sh"},
            }
        )

    wrapper = Path(cfg["wrapper_path"])
    assert wrapper.is_file() and os.access(wrapper, os.X_OK), wrapper
    assert wrapper.name == "run-cli-in-sandbox.sh"
    # Молчаливый фолбэк был бы своей бедой: человек так и не узнает, что его
    # значение проигнорировано.
    assert "wrapper_path" in caplog.text


def test_wrapper_path_next_to_the_config_wins(isolated_env, tmp_path):
    """Своя обёртка рядом с config_server.yml находится — как на develop.

    В рабочей копии `config_server.yml` лежит в корне репозитория, поэтому такой
    якорь дословно повторяет прежнее поведение: относительный путь считался от
    `bundled_root()`, а он и был корнем.
    """
    own = tmp_path / "scripts" / "run-cli-in-sandbox.sh"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("#!/bin/sh\n", encoding="utf-8")
    own.chmod(0o755)

    cfg = build_swarm_config(
        {
            "config_path": str(tmp_path / "config_server.yml"),
            "swarm": {"enabled": True, "wrapper_path": "scripts/run-cli-in-sandbox.sh"},
        }
    )
    assert Path(cfg["wrapper_path"]) == own
