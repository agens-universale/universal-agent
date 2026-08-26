import logging
import os
from typing import Any, Dict

import yaml

from gc_core.config import apply_cli_overrides, load_config, sync_logging_flat_and_params
from gc_core.config_modules.env_interpolation import resolve_env_placeholders
from gc_core.bundled_assets import bundled_root, repo_root

logger = logging.getLogger(__name__)


# bundled_root() — единый резолвер АССЕТОВ (editable → packages/core/assets,
# wheel → site-packages). Относительные пути вида "./prompts/server_agent.md"
# резолвятся от него.
PROJECT_ROOT = str(bundled_root())


def _server_config_candidates() -> list[str]:
    """Где ищем `config_server.yml`, в порядке приоритета.

    Раньше был один кандидат — `bundled_root()/config_server.yml`, и до
    разбиения на пакеты это работало: ассеты лежали в корне репозитория, туда же
    дока велит класть файл. После переезда ассетов путь стал указывать в
    `packages/core/assets/`, файл в корне перестал находиться, и сервер молча
    поднимался на минимальном дефолте — **с `auth_token: None`, то есть без
    авторизации**. Ни строки в лог.
    """
    candidates = [os.path.join(os.getcwd(), "config_server.yml")]
    root = repo_root()
    if root is not None:
        candidates.append(str(root / "config_server.yml"))
    # Исторический путь: рядом с ассетами. Оставлен, чтобы не сломать установки,
    # где файл действительно положили туда.
    candidates.append(os.path.join(PROJECT_ROOT, "config_server.yml"))
    seen: set[str] = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


def load_server_config() -> Dict[str, Any]:
    """
    Загрузить конфиг REST-сервера из config_server.yml или SERVER_CONFIG_PATH.

    Возвращает словарь с секциями:
    - server
    - defaults
    - agent
    - logging (опционально)
    - agent_overrides (опционально)
    - threads_cleanup (опционально)
    """
    from gc_core.config_modules import env_vars
    if env_vars.env.server_config_path is not None:
        cfg_path = str(env_vars.env.server_config_path)
        searched = [cfg_path]
    else:
        searched = _server_config_candidates()
        cfg_path = next((c for c in searched if os.path.exists(c)), searched[-1])
    if not os.path.exists(cfg_path):
        # Минимальный дефолт, чтобы сервер мог стартовать и без файла. Раньше
        # эта ветка была молчаливой, и «конфиг не нашёлся» было не отличить от
        # «так и задумано» — а вместе с конфигом терялся и auth_token.
        logger.warning(
            "config_server.yml не найден — сервер стартует на минимальном "
            "дефолте БЕЗ авторизации (auth_token не задан). Просмотрены пути: %s. "
            "Явный путь задаётся переменной окружения SERVER_CONFIG_PATH.",
            ", ".join(searched),
        )
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
            "logging": {},
        }

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # ${ENV:VAR} / ${FILE:path} — как в основном config.yml, чтобы серверные
    # секреты (server.auth_token и пр.) не приходилось держать в файле открытым
    # текстом. Отсутствующий источник → пустая строка + warning, как везде.
    raw = resolve_env_placeholders(raw)

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
        "logging": raw.get("logging") or {},
        "swarm": swarm_cfg,
        "threads_cleanup": threads_cleanup_cfg,
    }


def build_agent_config_for_server(server_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Построить config_dict для агента при запуске через REST-сервер.

    Логика:
    - Берём базовый config.yml через load_config().
    - Если в server_cfg задана секция logging — мержим её в cfg["logging_params"]
      и синхронизируем плоские ключи (console_trace, save_logs, full_log_backend).
    - Если в server_cfg.agent задан server_agentmd_path:
        - резолвим путь относительно PROJECT_ROOT (если он относительный);
        - перезаписываем cfg["agent_md_path"] значением server_agentmd_path.
    - Применяем перекрытия из server_cfg.agent_overrides через apply_cli_overrides
      (тот же путь, что CLI-аргументы) — так agent_overrides.model/completions_url
      тоже попадают в cli_explicit_keys и детектируются cli_forces_legacy_model
      (легаси-форс §2: перекрытие модели поверх непустого реестра моделей).
    """
    cfg = load_config()

    log_block = server_cfg.get("logging")
    if isinstance(log_block, dict) and log_block:
        lp = dict(cfg.get("logging_params") or {})
        lp.update(log_block)
        cfg["logging_params"] = lp
        for key in (
            "full_log_backend",
            "console_trace",
            "save_logs",
        ):
            if key in log_block:
                cfg[key] = log_block[key]

    agent_cfg = server_cfg.get("agent") or {}
    server_agent_path = agent_cfg.get("server_agentmd_path")
    if isinstance(server_agent_path, str) and server_agent_path.strip():
        if not os.path.isabs(server_agent_path):
            server_agent_path = os.path.join(PROJECT_ROOT, server_agent_path)
        cfg["agent_md_path"] = server_agent_path

    overrides = server_cfg.get("agent_overrides") or {}
    if isinstance(overrides, dict) and overrides:
        apply_cli_overrides(cfg, extra_overrides=overrides)

    sync_logging_flat_and_params(cfg)

    # В серверном режиме ограничиваем проектные метаданные,
    # чтобы не передавать структуру файловой системы пользователя.
    cfg.setdefault("limit_project_metadata", True)

    return cfg


__all__ = ["load_server_config", "build_agent_config_for_server", "PROJECT_ROOT"]

