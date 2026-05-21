import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config_modules.intent_scope import IntentScopeConfig, intent_scope_from_config_dict
from config_modules.summary import SummaryLlmConfig, summary_llm_from_summary_params
from utils.prod_auth import AuthConnector
from utils.logging_module import create_run_logger
from utils.logging_module import generate_run_id
from tools import build_tools_data
from config_modules.env_vars import project_gc_dir as _resolve_project_gc_dir


@dataclass
class RunContext:
    """
    Контекст одного запуска агента (CLI или REST‑запрос).

    Содержит:
    - config_dict — словарь конфигурации;
    - auth_connector — коннектор авторизации для бэкенда;
    - logger / logging_path — логгер и путь к файлу логов для этого запуска;
    - summary_trace_path — отдельный файл trace для суммаризации;
    - summary_llm — LLM-параметры суммаризации (из summary.llm в config);
    - intent_scope — параметры intent-scope hook (из intent_scope в config);
    - tools_data / tools_spec / tools_description — подготовленные данные о тулах;
    - project_gc_dir — папка project-local артефактов (<cwd>/.gc/).
    """

    config_dict: Dict[str, Any]
    run_id: str
    auth_connector: Optional[AuthConnector]
    logger: logging.Logger
    logging_path: str
    summary_trace_path: str
    summary_llm: SummaryLlmConfig
    intent_scope: IntentScopeConfig
    tools_data: Dict[str, Dict[str, Any]]
    tools_spec: List[Dict[str, Any]]
    tools_description: List[str]
    project_gc_dir: Path = field(default_factory=lambda: Path("."))


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
    effective_run_id = run_id or generate_run_id()
    tools_data, tools_spec, tools_description = build_tools_data(config_dict)

    run_logger, logging_path, summary_trace_path = create_run_logger(
        config_dict, run_id=effective_run_id, agent_kind="main"
    )

    #run_logger.debug(
    #    "Tools specs for this run:\n\n%s",
    #    json.dumps(tools_spec, ensure_ascii=False, indent=2),
    #)

    raw_summary_llm = config_dict.get("summary_llm")
    if isinstance(raw_summary_llm, SummaryLlmConfig):
        summary_llm = raw_summary_llm
    else:
        summary_llm = summary_llm_from_summary_params(config_dict.get("summary_params"))

    ctx = RunContext(
        config_dict=config_dict,
        run_id=effective_run_id,
        auth_connector=auth_connector,
        logger=run_logger,
        logging_path=logging_path,
        summary_trace_path=summary_trace_path,
        summary_llm=summary_llm,
        intent_scope=intent_scope_from_config_dict(config_dict),
        tools_data=tools_data,
        tools_spec=tools_spec,
        tools_description=tools_description,
        project_gc_dir=_resolve_project_gc_dir(),
    )

    run_logger.debug(
        "Created RunContext (run_id=%s, logging_path=%s)", effective_run_id, logging_path
    )

    return ctx


__all__ = [
    "RunContext",
    "create_run_context",
]

