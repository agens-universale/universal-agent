import os
from typing import Any, Dict

import yaml

from config import load_config  # type: ignore


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))


def load_server_config() -> Dict[str, Any]:
    """
    Загрузить конфиг REST-сервера из config_server.yml или SERVER_CONFIG_PATH.

    Возвращает словарь с секциями:
    - server
    - defaults
    - agent
    - agent_overrides (опционально)
    - threads_cleanup (опционально)
    """
    cfg_path = os.getenv("SERVER_CONFIG_PATH") or os.path.join(
        PROJECT_ROOT, "config_server.yml"
    )
    if not os.path.exists(cfg_path):
        # Минимальный дефолт, чтобы сервер мог стартовать и без файла.
        return {
            "server": {
                "host": "0.0.0.0",
                "port": 8000,
                "log_level": "info",
                "auth_token": None,
            },
            "defaults": {"system": "generic"},
            "agent": {"server_agentmd_path": "./prompts/server_agent.md"},
            "agent_overrides": {},
        }

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # Гарантируем наличие ключевых секций.
    server_cfg = raw.get("server") or {}
    defaults_cfg = raw.get("defaults") or {}
    agent_cfg = raw.get("agent") or {}
    overrides_cfg = raw.get("agent_overrides") or {}

    # Секция swarm — настройки сворм-режима (может отсутствовать в старых конфигах).
    swarm_cfg = raw.get("swarm") or {}

    threads_cleanup_cfg = raw.get("threads_cleanup") or {}

    return {
        "server": server_cfg,
        "defaults": defaults_cfg,
        "agent": agent_cfg,
        "agent_overrides": overrides_cfg,
        "swarm": swarm_cfg,
        "threads_cleanup": threads_cleanup_cfg,
    }


def build_agent_config_for_server(server_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Построить config_dict для агента при запуске через REST-сервер.

    Логика:
    - Берём базовый config.yml через load_config().
    - Если в server_cfg.agent задан server_agentmd_path:
        - резолвим путь относительно PROJECT_ROOT (если он относительный);
        - перезаписываем cfg["agent_md_path"] значением server_agentmd_path.
    - Применяем перекрытия из server_cfg.agent_overrides:
        - cfg.update(agent_overrides), как CLI-аргументы перекрывают config.yml.
    """
    cfg = load_config()

    agent_cfg = server_cfg.get("agent") or {}
    server_agent_path = agent_cfg.get("server_agentmd_path")
    if isinstance(server_agent_path, str) and server_agent_path.strip():
        if not os.path.isabs(server_agent_path):
            server_agent_path = os.path.join(PROJECT_ROOT, server_agent_path)
        cfg["agent_md_path"] = server_agent_path

    overrides = server_cfg.get("agent_overrides") or {}
    if isinstance(overrides, dict) and overrides:
        cfg.update(overrides)

    # В серверном режиме ограничиваем проектные метаданные,
    # чтобы не передавать структуру файловой системы пользователя.
    cfg.setdefault("limit_project_metadata", True)

    return cfg


__all__ = ["load_server_config", "build_agent_config_for_server", "PROJECT_ROOT"]

