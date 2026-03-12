import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from utils.prod_auth import AuthConnector
from utils.logging_module import create_run_logger
from tools import build_tools_data


@dataclass
class RunContext:
    """
    Контекст одного запуска агента (CLI или REST‑запрос).

    Содержит:
    - config_dict — словарь конфигурации;
    - auth_connector — коннектор авторизации для бэкенда;
    - logger / logging_path — логгер и путь к файлу логов для этого запуска;
    - tools_data / tools_spec / tools_description — подготовленные данные о тулах.
    """

    config_dict: Dict[str, Any]
    auth_connector: Optional[AuthConnector]
    logger: logging.Logger
    logging_path: str
    tools_data: Dict[str, Dict[str, Any]]
    tools_spec: List[Dict[str, Any]]
    tools_description: List[str]


def create_run_context(
    config_dict: Dict[str, Any],
    *,
    auth_connector: Optional[AuthConnector] = None,
    run_id: Optional[str] = None,
) -> RunContext:
    """
    Создать RunContext для одного запуска агента.

    - Тулы (tools_data, tools_spec, tools_description) кэшируются по ключу
      от config_dict, чтобы не пересобирать реестры при каждом запуске
      с одним и тем же конфигом.
    - Логгер создаётся заново для каждого запуска (своё имя файла логов).
    """
    tools_data, tools_spec, tools_description = build_tools_data(config_dict)

    run_logger, logging_path = create_run_logger(config_dict, run_id=run_id)

    run_logger.debug(
        "Tools specs for this run:\n\n%s",
        json.dumps(tools_spec, ensure_ascii=False, indent=2),
    )

    ctx = RunContext(
        config_dict=config_dict,
        auth_connector=auth_connector,
        logger=run_logger,
        logging_path=logging_path,
        tools_data=tools_data,
        tools_spec=tools_spec,
        tools_description=tools_description,
    )

    run_logger.debug(
        "Created RunContext (run_id=%s, logging_path=%s)", run_id, logging_path
    )

    return ctx


__all__ = [
    "RunContext",
    "create_run_context",
]

