import os
from typing import Any, Dict

import yaml

from config import apply_cli_overrides, load_config, sync_logging_flat_and_params  # type: ignore
from config_modules.env_interpolation import resolve_env_placeholders
from bundled_assets import bundled_root
from utils.user_text import read_user_text


# bundled_root() — единый резолвер ассетов (editable → repo, wheel → site-packages).
PROJECT_ROOT = str(bundled_root())


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
    from config_modules import env_vars
    if env_vars.env.server_config_path is not None:
        cfg_path = str(env_vars.env.server_config_path)
    else:
        cfg_path = os.path.join(PROJECT_ROOT, "config_server.yml")
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
            "logging": {},
        }

    # config_server.yml человек правит руками так же, как config.yml, — значит и
    # «файл на месте, но не читается» здесь такой же рабочий сценарий. Отказ должен
    # назвать файл: без пути оператор сервера видит голую жалобу кодека.
    raw = yaml.safe_load(read_user_text(cfg_path)) or {}

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

