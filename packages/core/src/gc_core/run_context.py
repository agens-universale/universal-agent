import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from gc_core.config_modules.intent_scope import IntentScopeConfig, intent_scope_from_config_dict
from gc_core.config_modules.chat_completions import ChatCompletionsLlmConfig, auxiliary_llm_from_config_dict
from gc_core.utils.prod_auth import AuthConnector
from gc_core.utils.logging_module import create_run_logger
from gc_core.utils.logging_module import generate_run_id
from gc_core.tools import build_tools_data
from gc_core.tools.skills.registry import SkillsRegistry, get_skills_registry
from gc_core.execution_workspace import ExecutionWorkspace, build_execution_workspace
from gc_core.backends.factory import resolve_os_backend
from gc_core.backends.protocol import FsBackend
from gc_core.config_modules.env_vars import project_gc_dir as _resolve_project_gc_dir
from gc_core.trace_id import trace_id_from_config, trace_header_name
from gc_core.model_router import CircuitBreaker, ModelRouter


@dataclass
class RunContext:
    """
    Контекст одного запуска агента (CLI или REST‑запрос).

    Содержит:
    - config_dict — словарь конфигурации;
    - auth_connector — коннектор авторизации для бэкенда;
    - logger / logging_path — логгер и путь к файлу логов для этого запуска;
    - summary_trace_path — отдельный файл trace для суммаризации;
    - auxiliary_llm — профиль вспомогательных LLM (summary, finish gate, judges);
    - intent_scope — параметры intent-scope hook (из intent_scope в config);
    - tools_data / tools_spec / tools_description — подготовленные данные о тулах;
    - project_gc_dir — папка project-local артефактов (<cwd>/.gc/);
    - os_backend — физический I/O порт (local FS или Harbor container);
    - workspace — root + backend в координатах среды исполнения;
    - model_router — резолвит consumer -> ResolvedModel (спека §6); фиксация на ход —
      см. turn_boundary.begin_turn.
    """

    config_dict: Dict[str, Any]
    run_id: str
    auth_connector: Optional[AuthConnector]
    logger: logging.Logger
    logging_path: str
    summary_trace_path: str
    auxiliary_llm: ChatCompletionsLlmConfig
    intent_scope: IntentScopeConfig
    tools_data: Dict[str, Dict[str, Any]]
    tools_spec: List[Dict[str, Any]]
    tools_description: List[str]
    os_backend: FsBackend
    skills_registry: SkillsRegistry
    workspace: ExecutionWorkspace
    model_router: ModelRouter
    # Внешний trace_id этого рана; "" = отсутствует. Производное чтение
    # config_dict["trace_id"] — источник правды один, вычисление в одном месте
    # (create_run_context), чтобы значения не разъехались.
    trace_id: str = ""
    mcp_status: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Имена тулов до вычитания stop_tools. Валидатор сверяется с этим набором:
    # успешно снятый skills_*/mcp_* не опечатка.
    registered_tool_names: Set[str] = field(default_factory=set)
    project_gc_dir: Path = field(default_factory=lambda: Path("."))
    environment: Optional[Any] = None
    on_stream_delta: Optional[Callable[[str, str], None]] = None
    # Optional: messages dropped by context compaction (not retained in State).
    on_messages_evicted: Optional[Callable[[List[Dict[str, Any]]], None]] = None


def create_run_context(
    config_dict: Dict[str, Any],
    *,
    auth_connector: Optional[AuthConnector] = None,
    run_id: Optional[str] = None,
    on_progress=None,
    environment: Optional[Any] = None,
    loop: Optional[Any] = None,
    os_backend: Optional[FsBackend] = None,
    on_stream_delta=None,
    on_messages_evicted: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    is_tui: bool = False,
    level_provider: Optional[Callable[[], str]] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    force_legacy: bool = False,
) -> RunContext:
    """
    Создать RunContext для одного запуска агента.

    - Тулы (tools_data, tools_spec, tools_description) кэшируются по ключу
      от config_dict, чтобы не пересобирать реестры при каждом запуске
      с одним и тем же конфигом.
    - Логгер создаётся заново для каждого запуска (своё имя файла логов).
    - ``os_backend`` собирается фабрикой из ``config_dict['os_backend']`` (default: local).
      Для ``harbor`` нужны ``environment`` и ``loop``; тесты могут передать ``os_backend=``.
    - ``is_tui=True`` только помечает контекст; diff-журнал включает сам TUI
      (``gc_tui.diff_tui.tracking.attach_to_context``) — ядро о нём не знает.
    - ``level_provider`` — источник активного уровня для ``model_router`` (спека §6.4).
      CLI/server не передают его → константный провайдер из ключа ``config_dict["level"]``.
      TUI (сессионный слой) передаёт живой callable. run_context не импортирует tui —
      инверсия зависимости через этот параметр.
    - ``circuit_breaker`` — предохранитель модели (спека §8); по умолчанию свежий.
    - ``force_legacy`` — легаси-форс (спека §2): CLI `--model`/`--completions_url`
      поверх непустого реестра моделей отключает резолв уровней/алиасов на этот
      запуск (детект — ``config.cli_forces_legacy_model``).
    """
    effective_run_id = run_id or generate_run_id()
    backend = resolve_os_backend(
        config_dict,
        environment=environment,
        loop=loop,
        override=os_backend,
    )
    skills_registry = get_skills_registry(config_dict, backend=backend)
    # Логгер создаётся ДО сборки тулов: она предупреждает о пропущенных файлах
    # сабагентов и нераспознанных именах тулов, и эти варнинги обязаны попасть в
    # лог-файл рана, а не только в stderr модульного логгера. Создание логгера
    # зависит лишь от config_dict и run_id, поэтому порядок безопасен.
    run_logger, logging_path, summary_trace_path = create_run_logger(
        config_dict, run_id=effective_run_id, agent_kind="main"
    )
    registered: list[str] = []
    tools_data, tools_spec, tools_description, mcp_status = build_tools_data(
        config_dict,
        on_progress=on_progress,
        os_backend=backend,
        skills_registry=skills_registry,
        logger=run_logger,
        registered_names=registered,
    )

    run_logger.info(
        "agent started: model=%s url=%s mode=%s",
        config_dict.get("model"),
        config_dict.get("completions_url"),
        config_dict.get("backend_api_mode"),
        extra={
            "event": "agent_start",
            "model": config_dict.get("model"),
            "url": config_dict.get("completions_url") or None,
            "backend_api_mode": config_dict.get("backend_api_mode"),
        },
    )

    run_logger.debug(
        "agent_tools",
        extra={
            "event": "agent_tools",
            "tools": sorted(tools_data.keys()),
            "count": len(tools_data),
        },
    )

    _trace_id = trace_id_from_config(config_dict)
    if _trace_id:
        # Одна явная запись «под какими именами этот ран шлёт trace». Заголовки
        # нигде не логируются, а имя заголовка конфигурируемо, поэтому это
        # единственное место, где аудит видит фактическую привязку.
        run_logger.info(
            "trace_binding",
            extra={
                "event": "trace_binding",
                "trace_id": _trace_id,
                "model_header": trace_header_name(config_dict, "x_trace_id"),
                "mcp_header": trace_header_name(config_dict, "trace_id"),
            },
        )

    auxiliary_llm = auxiliary_llm_from_config_dict(config_dict)
    workspace = build_execution_workspace(backend, config_dict)

    effective_level_provider = level_provider or (
        lambda: config_dict.get("level") or "default"
    )
    model_router = ModelRouter(
        config_dict,
        level_provider=effective_level_provider,
        breaker=circuit_breaker,
        force_legacy=force_legacy,
    )

    ctx = RunContext(
        config_dict=config_dict,
        run_id=effective_run_id,
        trace_id=_trace_id,
        auth_connector=auth_connector,
        logger=run_logger,
        logging_path=logging_path,
        summary_trace_path=summary_trace_path,
        auxiliary_llm=auxiliary_llm,
        intent_scope=intent_scope_from_config_dict(config_dict),
        tools_data=tools_data,
        tools_spec=tools_spec,
        tools_description=tools_description,
        mcp_status=mcp_status,
        registered_tool_names=set(registered),
        project_gc_dir=_resolve_project_gc_dir(),
        environment=environment,
        os_backend=backend,
        skills_registry=skills_registry,
        workspace=workspace,
        model_router=model_router,
        on_stream_delta=on_stream_delta,
        on_messages_evicted=on_messages_evicted,
    )
    ctx.is_tui = bool(is_tui)

    run_logger.debug(
        "Created RunContext (run_id=%s, logging_path=%s, os_backend=%s, workspace_root=%s)",
        effective_run_id,
        logging_path,
        type(ctx.os_backend).__name__,
        ctx.workspace.root,
    )

    return ctx


__all__ = [
    "RunContext",
    "create_run_context",
]
