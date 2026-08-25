import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from gc_core.config_modules.intent_scope import IntentScopeConfig, intent_scope_from_config_dict
from gc_core.config_modules.chat_completions import ChatCompletionsLlmConfig, chat_completions_llm_from_config_dict
from gc_core.utils.prod_auth import AuthConnector
from gc_core.utils.logging_module import create_run_logger
from gc_core.utils.logging_module import generate_run_id
from gc_core.tools import build_tools_data
from gc_core.mcp_session_owner import new_owner_token, use_owner
from gc_core.tools.skills.registry import SkillsRegistry, get_skills_registry
from gc_core.execution_workspace import ExecutionWorkspace, build_execution_workspace
from gc_core.backends.factory import resolve_os_backend
from gc_core.backends.protocol import FsBackend
from gc_core.config_modules.env_vars import project_gc_dir as _resolve_project_gc_dir
from gc_core.trace_id import trace_id_from_config, trace_header_name


@dataclass
class RunContext:
    """
    Контекст одного запуска агента (CLI или REST‑запрос).

    Содержит:
    - config_dict — словарь конфигурации;
    - auth_connector — коннектор авторизации для бэкенда;
    - logger / logging_path — логгер и путь к файлу логов для этого запуска;
    - summary_trace_path — отдельный файл trace для суммаризации;
    - chat_completions_llm — LLM-профиль (chat_completions.llm): summary, finish gate, intent scope, judges;
    - intent_scope — параметры intent-scope hook (из intent_scope в config);
    - tools_data / tools_spec / tools_description — подготовленные данные о тулах;
    - project_gc_dir — папка project-local артефактов (<cwd>/.gc/);
    - os_backend — физический I/O порт (local FS или Harbor container);
    - workspace — root + backend в координатах среды исполнения.
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
    skills_registry: SkillsRegistry
    workspace: ExecutionWorkspace
    # Внешний trace_id этого рана; "" = отсутствует. Производное чтение
    # config_dict["trace_id"] — источник правды один, вычисление в одном месте
    # (create_run_context), чтобы значения не разъехались.
    trace_id: str = ""
    mcp_status: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    project_gc_dir: Path = field(default_factory=lambda: Path("."))
    environment: Optional[Any] = None
    on_stream_delta: Optional[Callable[[str, str], None]] = None
    # Порт «спросить человека»: обычная функция или корутинная. Не задан —
    # тул turn_ask_human читает stdin (поведение CLI). Встраивающему приложению
    # stdin не принадлежит, поэтому спрашивать оно обязано своим способом.
    ask_human: Optional[Callable[[str], Any]] = None
    # Optional: messages dropped by context compaction (not retained in State).
    on_messages_evicted: Optional[Callable[[List[Dict[str, Any]]], None]] = None
    # Хранилище тредов. None — файловое по умолчанию (прежнее поведение).
    # Передав своё (например поверх базы), встраивающий уводит туда всю историю:
    # ни ThreadSession, ни ядро больше диска не касаются.
    thread_store: Any = None
    # ToolsCache этого контекста, если владелец его передал. Лежит здесь, чтобы
    # субагенты (они пересобирают tools_data на каждый спавн) брали готовое, а не
    # ходили в дискавери заново.
    tools_cache: Any = None
    # Владелец живых MCP-сессий этого контекста (см. gc_core.mcp_session_owner).
    # По нему ``arun`` понимает, ЧТО ему закрывать на выходе, и не сносит
    # сессии параллельных агентов. Пустая строка — контекст собран в обход
    # create_run_context (тесты, ручная сборка); тогда владельца нет.
    mcp_session_owner: str = ""


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
    tools_cache: Any = None,
    log_handlers: Any = None,
    thread_store: Any = None,
) -> RunContext:
    """
    Создать RunContext для одного запуска агента.

    - Тулы (tools_data, tools_spec, tools_description) собираются заново на
      каждый вызов. Кэш MCP-дискавери есть, но им ВЛАДЕЕТ ВЫЗЫВАЮЩИЙ: передайте
      ``tools_cache=ToolsCache(...)``, и повторные сборки с тем же набором
      ``mcp-servers`` перестанут ходить в сеть (866–1405 мс на реальном
      конфиге). Не передали — поведение прежнее, каждая сборка платит полностью.
    - Логгер создаётся заново для каждого запуска (своё имя файла логов).
    - ``os_backend`` собирается фабрикой из ``config_dict['os_backend']`` (default: local).
      Для ``harbor`` нужны ``environment`` и ``loop``; тесты могут передать ``os_backend=``.
    - ``is_tui=True`` + LocalFsBackend → ``TrackingFsBackend`` (Diff journal/snapshot).
    """
    prep = _prepare_run_context(
        config_dict, run_id=run_id, environment=environment, loop=loop,
        os_backend=os_backend, log_handlers=log_handlers,
    )
    with use_owner(prep.mcp_session_owner):
        tools = build_tools_data(
            config_dict,
            on_progress=on_progress,
            os_backend=prep.backend,
            skills_registry=prep.skills_registry,
            logger=prep.run_logger,
            tools_cache=tools_cache,
        )
    return _finish_run_context(
        config_dict, prep, tools,
        auth_connector=auth_connector, environment=environment,
        on_stream_delta=on_stream_delta, on_messages_evicted=on_messages_evicted,
        is_tui=is_tui, tools_cache=tools_cache, thread_store=thread_store,
    )


async def _acreate_run_context_impl(
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
    tools_cache: Any = None,
    log_handlers: Any = None,
    thread_store: Any = None,
) -> RunContext:
    """Тело асинхронной сборки: то же, что синхронное, но дискавери — await."""
    from gc_core.tools import abuild_tools_data

    prep = _prepare_run_context(
        config_dict, run_id=run_id, environment=environment, loop=loop,
        os_backend=os_backend, log_handlers=log_handlers,
    )
    with use_owner(prep.mcp_session_owner):
        tools = await abuild_tools_data(
            config_dict,
            on_progress=on_progress,
            os_backend=prep.backend,
            skills_registry=prep.skills_registry,
            logger=prep.run_logger,
            tools_cache=tools_cache,
        )
    return _finish_run_context(
        config_dict, prep, tools,
        auth_connector=auth_connector, environment=environment,
        on_stream_delta=on_stream_delta, on_messages_evicted=on_messages_evicted,
        is_tui=is_tui, tools_cache=tools_cache, thread_store=thread_store,
    )


@dataclass
class _RunContextPrep:
    """Всё, что готово ДО сборки тулов. Одинаково для обеих форм вызова."""

    run_id: str
    backend: FsBackend
    skills_registry: Any
    run_logger: Any
    logging_path: str
    summary_trace_path: str
    mcp_session_owner: str


def _prepare_run_context(
    config_dict: Dict[str, Any],
    *,
    run_id: Optional[str],
    environment: Optional[Any],
    loop: Optional[Any],
    os_backend: Optional[FsBackend],
    log_handlers: Any = None,
) -> _RunContextPrep:
    """Бэкенд, реестр скиллов, логгер и токен владельца MCP-сессий.

    Вынесено, чтобы синхронная и асинхронная сборка делили одно тело: между
    ними отличается ровно один шаг — как дождаться дискавери.
    """
    effective_run_id = run_id or generate_run_id()
    backend = resolve_os_backend(
        config_dict, environment=environment, loop=loop, override=os_backend,
    )
    skills_registry = get_skills_registry(config_dict, backend=backend)
    # Логгер создаётся ДО сборки тулов: она предупреждает о пропущенных файлах
    # сабагентов и нераспознанных именах тулов, и эти варнинги обязаны попасть в
    # лог-файл рана, а не только в stderr модульного логгера. Создание логгера
    # зависит лишь от config_dict и run_id, поэтому порядок безопасен.
    run_logger, logging_path, summary_trace_path = create_run_logger(
        config_dict, run_id=effective_run_id, agent_kind="main",
        log_handlers=log_handlers,
    )
    # Токен владельца MCP-сессий рождается здесь и живёт столько же, сколько
    # контекст. Дискавери поднимает сессии прямо сейчас, поэтому она обязана
    # идти уже под ним — иначе поднятое ею оказалось бы ничьим, и ни один
    # прогон не смог бы это закрыть.
    return _RunContextPrep(
        run_id=effective_run_id,
        backend=backend,
        skills_registry=skills_registry,
        run_logger=run_logger,
        logging_path=logging_path,
        summary_trace_path=summary_trace_path,
        mcp_session_owner=new_owner_token(),
    )


def _finish_run_context(
    config_dict: Dict[str, Any],
    prep: "_RunContextPrep",
    tools: tuple,
    *,
    auth_connector: Optional[AuthConnector],
    environment: Optional[Any],
    on_stream_delta,
    on_messages_evicted: Optional[Callable[[List[Dict[str, Any]]], None]],
    is_tui: bool,
    tools_cache: Any,
    thread_store: Any = None,
) -> RunContext:
    """Досборка после тулов: логи старта, trace-binding, сам RunContext."""
    # Хранилище встраивающего проверяем ЗДЕСЬ — это и есть точка проводки.
    # Раньше негодный объект принимался молча, а падал на 630-й строке
    # thread_store.py с «TypeError: cannot unpack non-iterable coroutine
    # object»: сообщение верное, но указывает не туда, и половина обращений
    # при этом вообще не падала, а теряла запись без ошибки и без лога.
    if thread_store is not None:
        from gc_core.thread_store_async import athread_store

        athread_store(thread_store)

    tools_data, tools_spec, tools_description, mcp_status = tools
    effective_run_id = prep.run_id
    backend = prep.backend
    skills_registry = prep.skills_registry
    run_logger = prep.run_logger
    logging_path = prep.logging_path
    summary_trace_path = prep.summary_trace_path
    mcp_session_owner = prep.mcp_session_owner
    run_id = effective_run_id
    os_backend = backend

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

    chat_completions_llm = chat_completions_llm_from_config_dict(config_dict)
    workspace = build_execution_workspace(backend, config_dict)

    ctx = RunContext(
        config_dict=config_dict,
        run_id=effective_run_id,
        trace_id=_trace_id,
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
        skills_registry=skills_registry,
        workspace=workspace,
        on_stream_delta=on_stream_delta,
        on_messages_evicted=on_messages_evicted,
        mcp_session_owner=mcp_session_owner,
        tools_cache=tools_cache,
        thread_store=thread_store,
    )
    ctx.is_tui = bool(is_tui)

    if is_tui and os_backend is None:
        # diff_tui едет вместе с extra [tui]: в headless-профиле пакета нет.
        # Ядро обязано это пережить — без diff-журнала прогон полностью
        # работоспособен, поэтому деградируем в no-op, а не роняем сборку
        # контекста (тот же приём, что в mcp_daemon_autostart).
        try:
            from gc_tui.diff_tui.tracking import wrap_local_for_tui
        except ImportError:
            wrap_local_for_tui = None

        if wrap_local_for_tui is not None:
            wrapped = wrap_local_for_tui(backend, ctx)
            if wrapped is not backend:
                ctx.os_backend = wrapped
                ctx.workspace = build_execution_workspace(wrapped, config_dict)

    run_logger.debug(
        "Created RunContext (run_id=%s, logging_path=%s, os_backend=%s, workspace_root=%s)",
        effective_run_id,
        logging_path,
        type(ctx.os_backend).__name__,
        ctx.workspace.root,
    )

    return ctx


async def acreate_run_context(config_dict: Dict[str, Any], **kwargs: Any) -> RunContext:
    """Собрать RunContext, не блокируя event loop.

    Настоящая корутина, а не ``to_thread`` над синхронной сборкой. Прежняя
    форма работала (loop был свободен), но стоила дорого: замер давал 8 event
    loop'ов и пик в 10 потоков на ОДНУ сборку — воркер пула, внутри него
    ``asyncio.run``, а под ним по демон-потоку на сервер и ещё по ``asyncio.run``
    в каждом. Нативный путь даёт ту же латентность при 1 loop и 5 потоках.

    И уходит ловушка: отмена ожидания возвращала управление не сразу, а через
    сотни миллисекунд — ``asyncio.run`` дожидался брошенного воркера
    дефолтного пула (``shutdown_default_executor``). Это ровно то, что уровнем
    ниже запрещает ``abandonable``, а на уровне контекста возвращалось обратно.

    Дорогая часть здесь одна — MCP-дискавери; остальное занимает 2.4–6.9 мс и
    остаётся синхронным, потому что это словари в памяти и короткий локальный
    I/O (лог-файл, проба прав на запись в корне проекта).
    """
    return await _acreate_run_context_impl(config_dict, **kwargs)


__all__ = [
    "RunContext",
    "acreate_run_context",
    "create_run_context",
]
