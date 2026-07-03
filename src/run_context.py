import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config_modules.intent_scope import IntentScopeConfig, intent_scope_from_config_dict
from config_modules.chat_completions import (
    ChatCompletionsLlmConfig,
    chat_completions_llm_from_config_dict,
)
from utils.prod_auth import AuthConnector
from utils.logging_module import create_run_logger
from utils.logging_module import generate_run_id
from tools import build_tools_data
from tools.backends.factory import resolve_os_backend
from tools.backends.protocol import FsBackend
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
    - chat_completions_llm — LLM-профиль (chat_completions.llm): summary, finish gate, intent scope;
    - intent_scope — параметры intent-scope hook (из intent_scope в config);
    - tools_data / tools_spec / tools_description — подготовленные данные о тулах;
    - project_gc_dir — папка project-local артефактов (<cwd>/.gc/);
    - os_backend — физический I/O порт (local FS или Harbor container).
    """

    config_dict: Dict[str, Any]
    run_id: str
    auth_connector: Optional[AuthConnector]
    logger: logging.Logger
    logging_path: str
    summary_trace_path: str
    chat_completions_llm: ChatCompletionsLlmConfig
    intent_scope: IntentScopeConfig
    tools_data: Dict[str, Dict[str, Any]]
    tools_spec: List[Dict[str, Any]]
    tools_description: List[str]
    os_backend: FsBackend
    mcp_status: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    project_gc_dir: Path = field(default_factory=lambda: Path("."))
    environment: Optional[Any] = None


def create_run_context(
    config_dict: Dict[str, Any],
    *,
    auth_connector: Optional[AuthConnector] = None,
    run_id: Optional[str] = None,
    on_progress=None,
    environment: Optional[Any] = None,
    loop: Optional[Any] = None,
    os_backend: Optional[FsBackend] = None,
) -> RunContext:
    """
    Создать RunContext для одного запуска агента.

    - Тулы (tools_data, tools_spec, tools_description) кэшируются по ключу
      от config_dict, чтобы не пересобирать реестры при каждом запуске
      с одним и тем же конфигом.
    - Логгер создаётся заново для каждого запуска (своё имя файла логов).
    - ``os_backend`` собирается фабрикой из ``config_dict['os_backend']`` (default: local).
      Для ``harbor`` нужны ``environment`` и ``loop``; тесты могут передать ``os_backend=``.
    """
    effective_run_id = run_id or generate_run_id()
    tools_data, tools_spec, tools_description, mcp_status = build_tools_data(config_dict, on_progress=on_progress)

    run_logger, logging_path, summary_trace_path = create_run_logger(
        config_dict, run_id=effective_run_id, agent_kind="main"
    )

    #run_logger.debug(
    #    "Tools specs for this run:\n\n%s",
    #    json.dumps(tools_spec, ensure_ascii=False, indent=2),
    #)

    chat_completions_llm = chat_completions_llm_from_config_dict(config_dict)
    backend = resolve_os_backend(
        config_dict,
        environment=environment,
        loop=loop,
        override=os_backend,
    )

    ctx = RunContext(
        config_dict=config_dict,
        run_id=effective_run_id,
        auth_connector=auth_connector,
        logger=run_logger,
        logging_path=logging_path,
        summary_trace_path=summary_trace_path,
        chat_completions_llm=chat_completions_llm,
        intent_scope=intent_scope_from_config_dict(config_dict),
        tools_data=tools_data,
        tools_spec=tools_spec,
        tools_description=tools_description,
        mcp_status=mcp_status,
        project_gc_dir=_resolve_project_gc_dir(),
        environment=environment,
        os_backend=backend,
    )

    run_logger.debug(
        "Created RunContext (run_id=%s, logging_path=%s, os_backend=%s)",
        effective_run_id,
        logging_path,
        type(backend).__name__,
    )

    return ctx


__all__ = [
    "RunContext",
    "create_run_context",
]
