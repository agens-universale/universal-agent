from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set

from gc_core.agent_store import (
    AgentStore,
    FileAgentStore,
    check_agent_store,
    project_key_for,
)
from gc_core.attachments.store import ImageStore, image_store_for
from gc_core.auto_routing.levels import (
    auto_available,
    effective_level,
    resolve_routing_params,
)
from gc_core.auto_routing.policy import RoutingParams
from gc_core.auto_routing.request_equivalence import PreparedRequestEquivalence
from gc_core.backends.factory import resolve_os_backend
from gc_core.backends.protocol import FsBackend
from gc_core.config_modules.chat_completions import (
    ChatCompletionsLlmConfig,
    auxiliary_llm_from_config_dict,
)
from gc_core.config_modules.env_vars import (
    project_gc_dir as _resolve_project_gc_dir,
)
from gc_core.config_modules.intent_scope import (
    IntentScopeConfig,
    intent_scope_from_config_dict,
)
from gc_core.context_preparation import PreparedRunContext, RunContextBindings
from gc_core.execution.contracts import ToolEffectExecutor
from gc_core.execution.guard_contracts import GuardProbe
from gc_core.execution.human import HumanRequester
from gc_core.execution.ports import AgentOperations
from gc_core.execution_workspace import (
    ExecutionWorkspace,
    aprime_backend_cwd,
    areconcile_remote_root,
    build_execution_workspace,
)
from gc_core.mcp_session_owner import new_owner_token, use_owner
from gc_core.model_router import (
    CircuitBreaker,
    ModelRouter,
    main_model_identity,
)
from gc_core.run_preparation import PromptLoader
from gc_core.startup_notices import auto_routing_unavailable_notice
from gc_core.state_preparation import StatePreparer
from gc_core.tools import abuild_tools_data
from gc_core.tools.skills.registry import SkillsRegistry
from gc_core.trace_id import trace_header_name, trace_id_from_config
from gc_core.utils.logging_module import create_run_logger, generate_run_id
from gc_core.utils.prod_auth import AuthConnector


@dataclass
class RunContext:
    """
    Контекст одного запуска агента (CLI или REST‑запрос).

    Содержит:
    - settings / runtime — настройки (``AppConfig``) и рантайм (``RuntimeEnv``);
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
    # runtime.trace_id — источник правды один, вычисление в одном месте
    # (create_run_context), чтобы значения не разъехались.
    operations: Optional[AgentOperations] = None
    # None keeps local file loading. External runtimes supply prepared values
    # without changing prompt assembly or serializing live clients/loggers.
    prompt_loader: Optional[PromptLoader] = None
    state_preparer: Optional[StatePreparer] = None
    human_request: Optional[HumanRequester] = None
    guard_probe: Optional[GuardProbe] = None
    tool_effect: Optional[ToolEffectExecutor] = None
    lock_factory: Any = None
    skills_loader: Any = None
    mcp_cleanup: Any = None
    prompt_resource: Any = None
    project_memory_io: Any = None
    summary_trace_sink: Any = None
    collect_garbage: Any = None
    trace_id: str = ""
    mcp_status: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Имена тулов до вычитания stop_tools. Валидатор сверяется с этим набором:
    # успешно снятый skills_*/mcp_* не опечатка.
    registered_tool_names: Set[str] = field(default_factory=set)
    project_gc_dir: Path = field(default_factory=lambda: Path("."))
    environment: Optional[Any] = None
    on_stream_delta: Optional[Callable[[str, str], None]] = None
    # Пометка в ленте о судьбе изображения («не просмотрено», спека изображений
    # §10): текст готов к показу. Не задан — `vision.notes.emit_trace_note`
    # пишет предупреждение в `logger` (CLI, сервер, SDK).
    on_trace_note: Optional[Callable[[str], None]] = None
    # Порт «спросить человека»: обычная функция или корутинная. Не задан —
    # тул turn_ask_human читает stdin (поведение CLI). Встраивающему приложению
    # stdin не принадлежит, поэтому спрашивать оно обязано своим способом.
    ask_human: Optional[Callable[[str], Any]] = None
    # Optional: messages dropped by context compaction (not retained in State).
    on_messages_evicted: Optional[Callable[[List[Dict[str, Any]]], None]] = (
        None
    )
    # Активный скоуп ввода субагента ЭТОГО прогона: сюда уходит сообщение
    # пользователя, пришедшее посреди работы ребёнка. Живёт здесь, а не в
    # глобали модуля, потому что процесс ведёт МНОГО прогонов сразу (сервер) —
    # и глобаль отдавала сообщение тому, кто зарегистрировался последним, чужой
    # сессии в том числе. Подробности — в `tools/subagents/active_scope`.
    child_input_scope: Optional[Any] = None
    # Хранилище тредов. None — файловое по умолчанию (прежнее поведение).
    # Передав своё (например поверх базы), встраивающий уводит туда всю историю:
    # ни ThreadSession, ни ядро больше диска не касаются.
    thread_store: Any = None
    # Хранилище агента (``gc_core.agent_store``): промпты, AGENTS.md, скиллы,
    # субагенты — общее для всех тредов агента. Собранный фабрикой контекст
    # всегда его имеет (по умолчанию файловое); ``None`` — контекст собран в
    # обход фабрики, читатели берут файловое через ``agent_store_of``.
    agent_store: Any = None
    # ToolsCache этого контекста, если владелец его передал. Лежит здесь, чтобы
    # субагенты (они пересобирают tools_data на каждый спавн) брали готовое, а не
    # ходили в дискавери заново.
    tools_cache: Any = None
    # Владелец живых MCP-сессий этого контекста (см. gc_core.mcp_session_owner).
    # По нему ``arun`` понимает, ЧТО ему закрывать на выходе, и не сносит
    # сессии параллельных агентов. Пустая строка — контекст собран в обход
    # create_run_context (тесты, ручная сборка); тогда владельца нет.
    mcp_session_owner: str = ""
    #: Типизированные настройки (``AppConfig``) и рантайм (``RuntimeEnv``).
    settings: Any = None
    runtime: Any = None
    #: Определения кастомных субагентов этого контекста (кэш дискавери) и
    #: диагностика их загрузки. ``None`` — ещё не собраны; durable-сборка
    #: (Temporal) кладёт сюда готовые определения, чтобы workflow не ходил на диск.
    subagent_definitions: Any = None
    subagent_diagnostics: Any = None
    # Хранилище картинок (`gc_core.attachments.store`): проекция читает байты
    # вложений через `read_for_send`, ядро делает `commit` в тред. Заполняет
    # `_finish_run_context`; контекст, собранный в обход фабрики (в том числе
    # durable-сборка в workflow), — None: живые картинки уходят запиской
    # «файл недоступен», а файловых операций в workflow нет.
    image_store: Optional[ImageStore] = None

    active_state: Any = field(default=None, repr=False)
    # Доступна ли фича auto этому запуску (спека §5.1) — считается ОДИН раз при
    # сборке ctx (`acreate_run_context`, конфиг рана не меняется) и кладётся сюда;
    # `aroute_before_call` читает готовое значение вместо повторного вычисления из
    # `config_dict` на каждый вызов (единая точка предиката, без отдельной ветки
    # на случай отсутствия поля).
    auto_available: bool = True
    # Параметры двухуровневого автовыбора: безопасный уровень, длины серий
    # и порог пары. Считаются один раз при сборке ctx, как
    # `auto_available`; обе точки сборки RunContext кладут их сами.
    routing_params: RoutingParams = field(default_factory=RoutingParams)
    # Старые durable-истории сохраняют прежнюю карточку и сравнение комплектов.
    routing_contract_v2: bool = True
    prepared_request_equivalence: PreparedRequestEquivalence | None = None
    subagent_independent_auto: bool = True
    # Версия поведения исполнения: durable-рантайм выбирает ветку по истории;
    # локально отсутствие порта означает текущее поведение.
    execution_patch: Optional[Callable[[str], bool]] = None

    def attach_state(self, state: Any) -> None:
        """Привязать State рана к контексту — единственная точка записи
        `active_state` (спека auto-routing §5.1)."""
        self.active_state = state


def _coerce_settings(settings, runtime):
    """``LoadedSettings`` сборки из ``LoadedSettings`` или ``AppConfig``.

    Явный ``runtime`` вызывающего главнее рантайма загрузки. Плоский словарь
    не принимается: ``TypeError``.
    """
    from gc_core.settings import AppConfig, LoadedSettings, RuntimeEnv

    if isinstance(settings, LoadedSettings):
        if runtime is not None and runtime is not settings.runtime:
            settings = dataclasses.replace(settings, runtime=runtime)
        return settings
    if isinstance(settings, AppConfig):
        return LoadedSettings(
            settings=settings, runtime=runtime or RuntimeEnv()
        )
    raise TypeError(
        "acreate_run_context принимает LoadedSettings или AppConfig, "
        f"а не {type(settings).__name__}"
    )


def assemble_run_context(
    prepared: PreparedRunContext, bindings: RunContextBindings
) -> RunContext:
    """Build the real RunContext from prepared values and explicit resources.

    Both local creation and durable execution use this constructor. Discovery,
    workspace probes, logging files and provider clients belong to preparation.
    """
    if prepared.schema_version != 2:
        raise ValueError("Unsupported PreparedRunContext schema_version")
    # Настройки — из подготовленных значений: локально это сами объекты,
    # durable-сборка (Temporal) получает публичный JSON-снимок.
    from gc_core.context_preparation import settings_from_snapshot

    loaded = settings_from_snapshot(prepared.settings, prepared.runtime)
    workspace = ExecutionWorkspace(
        root=prepared.workspace_root,
        backend=bindings.os_backend,
        shell=prepared.workspace_shell,
        os_name=prepared.workspace_os_name,
        root_writable=prepared.workspace_root_writable,
    )
    ctx = RunContext(
        settings=loaded.settings,
        runtime=loaded.runtime,
        run_id=prepared.run_id,
        trace_id=prepared.trace_id,
        auth_connector=bindings.auth_connector,
        logger=bindings.logger,
        logging_path=prepared.logging_path,
        summary_trace_path=prepared.summary_trace_path,
        auxiliary_llm=auxiliary_llm_from_config_dict(loaded),
        intent_scope=intent_scope_from_config_dict(loaded),
        tools_data=bindings.tools_data,
        tools_spec=prepared.tools_spec,
        tools_description=prepared.tools_description,
        mcp_status=prepared.mcp_status,
        registered_tool_names=set(prepared.registered_tool_names),
        project_gc_dir=Path(prepared.project_gc_dir),
        environment=bindings.environment,
        os_backend=bindings.os_backend,
        skills_registry=bindings.skills_registry,
        workspace=workspace,
        model_router=bindings.model_router,
        on_stream_delta=bindings.on_stream_delta,
        on_trace_note=bindings.on_trace_note,
        on_messages_evicted=bindings.on_messages_evicted,
        mcp_session_owner=prepared.mcp_session_owner,
        tools_cache=bindings.tools_cache,
        thread_store=bindings.thread_store,
        agent_store=bindings.agent_store,
        operations=bindings.operations,
        execution_patch=bindings.execution_patch,
        prompt_loader=bindings.prompt_loader,
        state_preparer=bindings.state_preparer,
        human_request=bindings.human_request,
        guard_probe=bindings.guard_probe,
        tool_effect=bindings.tool_effect,
        lock_factory=bindings.lock_factory,
        skills_loader=bindings.skills_loader,
        mcp_cleanup=bindings.mcp_cleanup,
        prompt_resource=bindings.prompt_resource,
        project_memory_io=bindings.project_memory_io,
        summary_trace_sink=bindings.summary_trace_sink,
        collect_garbage=bindings.collect_garbage,
        prepared_request_equivalence=bindings.prepared_request_equivalence,
        subagent_independent_auto=bindings.subagent_independent_auto,
    )
    ctx.is_tui = prepared.is_tui
    ctx.routing_contract_v2 = (
        bindings.execution_patch is None
        or bindings.execution_patch("routing-request-contract-v2")
    )
    if ctx.prepared_request_equivalence is not None:
        from gc_core.auto_routing.request_equivalence import (
            validate_request_equivalence,
        )

        validate_request_equivalence(
            ctx, ctx.prepared_request_equivalence, context_id=ctx.run_id
        )
    ctx.auto_available = auto_available(
        ctx,
        force_legacy=(
            bindings.model_router.force_legacy
            or not bindings.model_router.auto_allowed
        ),
        legacy_call_kits=not ctx.routing_contract_v2,
    )
    ctx.routing_params = resolve_routing_params(
        ctx, legacy_call_kits=not ctx.routing_contract_v2
    )
    live = bindings.model_router.live_level

    def effective_provider():
        state = ctx.active_state
        return effective_level(
            live(), getattr(state, "current_level", None), ctx.auto_available
        )

    ctx.model_router = bindings.model_router.with_level_providers(
        effective_provider, live
    )
    return ctx


async def acreate_run_context(  # noqa: PLR0913  # S107: won't fix, см. CONTRIBUTING.md
    settings: Any,
    *,
    runtime: Any = None,
    auth_connector: Optional[AuthConnector] = None,
    run_id: Optional[str] = None,
    on_progress=None,
    environment: Optional[Any] = None,
    loop: Optional[Any] = None,
    os_backend: Optional[FsBackend] = None,
    on_stream_delta=None,
    on_trace_note: Optional[Callable[[str], None]] = None,
    on_messages_evicted: Optional[
        Callable[[List[Dict[str, Any]]], None]
    ] = None,
    is_tui: bool = False,
    tools_cache: Any = None,
    log_handlers: Any = None,
    thread_store: Any = None,
    level_provider: Optional[Callable[[], str]] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    force_legacy: bool = False,
    prompt_loader: Optional[PromptLoader] = None,
    state_preparer: Optional[StatePreparer] = None,
    human_request: Optional[HumanRequester] = None,
    guard_probe: Optional[GuardProbe] = None,
    tool_effect: Optional[ToolEffectExecutor] = None,
    agent_store: Optional[AgentStore] = None,
) -> RunContext:
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

    Синхронного двойника у сборки БОЛЬШЕ НЕТ. Он держался ради TUI, и docs
    объясняли его привязкой MCP-сессий к loop'у — обоснование было неверным:
    синхронная форма уводила дискавери в `run_coro_blocking`, то есть на
    такой же одноразовый loop. Настоящая причина была в том, что фабрика
    исполнителя в TUI — обычная функция без своего loop; теперь она заводит
    его сама (`asyncio.run`), как уже делает на каждый ход.


    Создать RunContext для одного запуска агента.

    - Тулы (tools_data, tools_spec, tools_description) собираются заново на
      каждый вызов. Кэш MCP-дискавери есть, но им ВЛАДЕЕТ ВЫЗЫВАЮЩИЙ: передайте
      ``tools_cache=ToolsCache(...)``, и повторные сборки с тем же набором
      ``mcp-servers`` перестанут ходить в сеть (866–1405 мс на реальном
      конфиге). Не передали — поведение прежнее, каждая сборка платит полностью.
    - Логгер создаётся заново для каждого запуска (своё имя файла логов).
    - ``os_backend`` собирается фабрикой по ``runtime.os_backend`` (default: local).
      Для ``harbor`` нужны ``environment`` и ``loop``; тесты могут передать ``os_backend=``.
    - Свой LocalFsBackend → ``TrackingFsBackend`` (журнал правок) там, где
      запись включена (``agent_edits.record``: TUI по умолчанию, CLI и сервер —
      по конфигу); библиотечный вызов без режима запуска не оборачивается.
    - ``level_provider`` — источник активного уровня для ``model_router`` (спека §6.4).
      CLI/server не передают его → константный провайдер из ``settings.level``.
      TUI (сессионный слой) передаёт живой callable. run_context не импортирует tui —
      инверсия зависимости через этот параметр.
    - ``circuit_breaker`` — предохранитель модели (спека §8); по умолчанию свежий.
    - ``force_legacy`` — легаси-форс (спека §2): CLI `--model`/`--completions_url`
      поверх непустого реестра моделей отключает резолв уровней/алиасов на этот
      запуск (детект — ``config.cli_forces_legacy_model``).
    """
    # Хранилище встраивающего проверяем ДО всего остального. Проверка стояла в
    # `_finish_run_context`, то есть после MCP-дискавери и создания логгера: на
    # негодном сторе исключение прилетало, когда stdio-сессии уже подняты под
    # токеном владельца, а `AsyncAgent.create` на ошибке закрывает только пул
    # httpx — persistent stdio (браузер) и логгер с открытым файлом доживали до
    # конца процесса.
    # Нефайловый storage: без открытого хранилища — отказ до всего.
    from gc_core.stores import require_opened_storage

    require_opened_storage(
        _coerce_settings(settings, runtime),
        thread_store=thread_store,
        agent_store=agent_store,
    )
    if thread_store is not None:
        from gc_core.thread_store_async import athread_store

        athread_store(thread_store)
    # Хранилище агента — там же и по той же причине: до дискавери.
    if agent_store is not None:
        check_agent_store(agent_store)

    source = _coerce_settings(settings, runtime)
    prep = _prepare_run_context(
        source,
        run_id=run_id,
        environment=environment,
        loop=loop,
        os_backend=os_backend,
        log_handlers=log_handlers,
        # Файловое по умолчанию — по настройкам этой сборки (с runtime-слоем
        # фронтенда: ACP, TUI и chat подменяют special_instructions).
        agent_store=agent_store or FileAgentStore(source),
    )
    # Пробы возможностей бэкенда (`has_grep`/`has_find`/…) здесь СОЗНАТЕЛЬНО не
    # прогреваются, хотя `awarm_capabilities` для этого есть.
    #
    # Во-первых, прогрев уводит пробы в поток, а сборка контекста обязана
    # обходиться без потоков — это отдельное свойство со своей историей
    # (прежняя форма давала 8 event loop'ов и пик в 10 потоков на ОДНУ сборку),
    # и стережёт его `test_context_build_native_async`.
    #
    # Во-вторых, прогрев плату не убирает, а переносит: пробы кэшируются и
    # стоят однократно на процесс. Прогрев при сборке заставил бы платить за
    # них КАЖДЫЙ прогон, включая те, что ни разу не ищут по файлам.
    #
    # Кому перенос нужен (фронтенд, знающий, что поиск будет) — зовёт
    # `backend.awarm_capabilities()` сам, вне горячего пути.
    # Рабочий каталог окружения спрашиваем ЗДЕСЬ, до дискавери MCP: сборка
    # рабочей области ниже синхронная, а синхронного `exec` у песочницы нет.
    # До дискавери — чтобы его отказ не застал открытых MCP-сессий.
    #
    # Страховка — на ВСЁ после подготовки, а не на один прогрев. Логгер
    # прогона уже создан и держит открытый файл, а дискавери поднимает
    # stdio-серверы под токеном владельца этого контекста. Обычный отказ
    # дискавери исключения не бросает (сервер помечается недоступным), но
    # отмена посреди неё и исключение сборки после неё раньше шли мимо:
    # логгер доживал до конца процесса, сервер (браузер) — до `atexit`.
    # Закрыть его раньше некому: снос фильтрует по владельцу, а контекста,
    # которому он принадлежит, так и не появилось.
    try:
        # sandbox.workdir=auto мог сменить корень рантайма в подготовке.
        source = prep.settings
        await aprime_backend_cwd(prep.backend, source)
        source = await areconcile_remote_root(prep.backend, source)
        prep.settings = source
        # Скиллы, которые надо доставить в среду исполнения (файловое
        # хранилище и remote-бэкенд), — реестр по доставленной копии.
        provisioned = await prep.agent_store.aprovision_skills(
            prep.backend,
            source,
            run_logger=prep.run_logger,
            project=prep.project,
        )
        if provisioned is not None:
            prep.skills_registry = provisioned
        # Каталог имён ДО вычитания stop_tools: валидатор сверяется с ним, иначе
        # успешно снятый тул снова считается опечаткой. Способность пришла с
        # develop, у нас её не было; тело, которое список заполняет, слилось
        # автоматически — без самого списка оно осталось бы мёртвым кодом.
        registered: list[str] = []
        # Источник сборки — двойник контекста: реестр субагентов запоминает на
        # нём найденные определения, и контекст получает их готовыми (иначе
        # дискавери шёл бы второй раз на первом вызове тула, с повтором
        # предупреждений).
        build_source = SimpleNamespace(
            settings=source.settings,
            runtime=source.runtime,
            subagent_definitions=None,
            subagent_diagnostics=None,
            agent_store=prep.agent_store,
        )
        with use_owner(prep.mcp_session_owner):
            tools = await abuild_tools_data(
                build_source,
                on_progress=on_progress,
                os_backend=prep.backend,
                skills_registry=prep.skills_registry,
                logger=prep.run_logger,
                tools_cache=tools_cache,
                registered_names=registered,
            )
        ctx = _finish_run_context(
            prep,
            tools,
            registered_names=registered,
            auth_connector=auth_connector,
            environment=environment,
            on_stream_delta=on_stream_delta,
            on_trace_note=on_trace_note,
            on_messages_evicted=on_messages_evicted,
            is_tui=is_tui,
            tools_cache=tools_cache,
            thread_store=thread_store,
            level_provider=level_provider,
            circuit_breaker=circuit_breaker,
            force_legacy=force_legacy,
            prompt_loader=prompt_loader,
            state_preparer=state_preparer,
            human_request=human_request,
            guard_probe=guard_probe,
            tool_effect=tool_effect,
        )
        # Локально — сами объекты сборки, а не их пересборка из снимка.
        ctx.settings, ctx.runtime = source.settings, source.runtime
        if ctx.subagent_definitions is None:
            ctx.subagent_definitions = build_source.subagent_definitions
            ctx.subagent_diagnostics = build_source.subagent_diagnostics
        return ctx
    except BaseException as exc:
        from gc_core.utils.logging_module import release_run_logger

        try:
            # Закрытую корутину (`GeneratorExit`) ждать уже нельзя: `await`
            # здесь кончился бы «coroutine ignored GeneratorExit». Тогда
            # серверы доберёт хук на выход, а логгер отдаём в любом случае.
            if not isinstance(exc, GeneratorExit):
                await _aclose_owned_mcp_sessions(prep.mcp_session_owner)
        finally:
            release_run_logger(prep.run_logger)
        raise


async def _aclose_owned_mcp_sessions(owner: str) -> None:
    """Закрыть MCP-сессии, поднятые под владельцем несостоявшегося контекста.

    Закрывается долгоживущий stdio, и этого достаточно: других живых сессий
    дискавери не оставляет. Stream-транспорты спрашивают список тулов
    одноразовой сессией, а долгоживущую заводит только вызов тула — до него
    сборка не доходит.

    Не ``_aclose_mcp_and_release`` из исполнителя, хотя по сути то же самое.
    Тот держит реестр, связанный при импорте ``agent_executor``, а дискавери
    берёт клиента лениво, в момент вызова. Реестр здесь берётся так же — и
    потому гарантированно тот, куда дискавери положила сессии. Это не
    теория: вторая копия ``gc_core.tools.*`` в процессе бывает — тесты
    ``tests/tools`` и ``tests/security`` импортируются под заглушкой пакета, а
    тест-кит до недавнего времени перезагружал его после каждого теста, — и с
    помощником исполнителя проверка этого места краснела уже на втором тесте
    файла: закрывался реестр другой копии модуля. К тому же сборка контекста
    лежит ниже исполнителя, и тянуть его наверх ради отказа незачем.
    """
    from gc_core.tools.connect_mcp.persistent_stdio import (
        PersistentStdioRegistry,
    )

    with use_owner(owner):
        await PersistentStdioRegistry.instance().aclose_all()


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
    #: Вызывающий передал СВОЙ бэкенд (а не положился на фабрику).
    #:
    #: Нужен на шаге досборки: при включённой записи правок ядро оборачивает
    #: локальный бэкенд в ``TrackingFsBackend`` только тогда, когда бэкенд
    #: собрали мы сами. Чужой не трогаем — вызывающий знает, что подсунул.
    #:
    #: Флагом, а не самим бэкендом: после ``resolve_os_backend`` отличить
    #: «передали свой» от «собрали дефолтный» уже нечем, и в первой редакции
    #: разделения это привело к тому, что ``os_backend is None`` в досборке
    #: сравнивалось с локальной переменной, всегда не-None. Условие стало
    #: вечно ложным, а обёртка — мёртвым кодом.
    backend_was_overridden: bool
    #: Настройки сборки (``LoadedSettings``): из них читает и досборка.
    settings: Any = None
    agent_store: Any = None
    #: Проект рана (``ProjectKey``): слой скиллов ``project`` и ресурсы
    #: проекта своего хранилища агента.
    project: Any = None


def _prepare_run_context(
    settings: Any,
    *,
    run_id: Optional[str],
    environment: Optional[Any],
    loop: Optional[Any],
    os_backend: Optional[FsBackend],
    log_handlers: Any = None,
    agent_store: AgentStore | None = None,
) -> _RunContextPrep:
    """Бэкенд, реестр скиллов, логгер и токен владельца MCP-сессий.

    Вынесено, чтобы синхронная и асинхронная сборка делили одно тело: между
    ними отличается ровно один шаг — как дождаться дискавери. ``settings`` —
    всё, что принимают ``settings_of``/``runtime_of`` (``LoadedSettings``).
    """
    effective_run_id = run_id or generate_run_id()
    backend = resolve_os_backend(
        settings,
        environment=environment,
        loop=loop,
        override=os_backend,
    )
    # sandbox.workdir=auto: каталог рана обязан стать launch_root — его читают
    # write_guard (`launch_root_from_config`) и workspace, а бэкенда у них нет.
    # Побеждает безусловно: TUI/ACP ставят launch_root=cwd ещё до сборки, и
    # без перезаписи guards пускали бы файловые тулы в реальный проект.
    auto_workdir = getattr(backend, "auto_workdir", None)
    if auto_workdir and hasattr(settings, "with_runtime"):
        settings = settings.with_runtime(
            launch_root=auto_workdir, project_root=auto_workdir
        )
    if agent_store is None:
        agent_store = FileAgentStore(settings)
    # Проект рана строится до контекста: реестр скиллов нужен раньше.
    project = project_key_for(str(_resolve_project_gc_dir().parent), settings)
    skills_registry = agent_store.skills_registry(
        settings, backend=backend, project=project
    )
    # Логгер создаётся ДО сборки тулов: она предупреждает о пропущенных файлах
    # сабагентов и нераспознанных именах тулов, и эти варнинги обязаны попасть в
    # лог-файл рана, а не только в stderr модульного логгера. Создание логгера
    # зависит лишь от настроек и run_id, поэтому порядок безопасен.
    run_logger, logging_path, summary_trace_path = create_run_logger(
        settings,
        run_id=effective_run_id,
        agent_kind="main",
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
        backend_was_overridden=os_backend is not None,
        settings=settings,
        agent_store=agent_store,
        project=project,
    )


def _finish_run_context(  # noqa: PLR0913  # S107: won't fix, см. CONTRIBUTING.md
    prep: _RunContextPrep,
    tools: tuple,
    *,
    registered_names: Optional[list] = None,
    auth_connector: Optional[AuthConnector],
    environment: Optional[Any],
    on_stream_delta,
    on_trace_note: Optional[Callable[[str], None]] = None,
    on_messages_evicted: Optional[Callable[[List[Dict[str, Any]]], None]],
    is_tui: bool,
    tools_cache: Any,
    thread_store: Any = None,
    level_provider: Optional[Callable[[], str]] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    force_legacy: bool = False,
    prompt_loader: Optional[PromptLoader] = None,
    state_preparer: Optional[StatePreparer] = None,
    human_request: Optional[HumanRequester] = None,
    guard_probe: Optional[GuardProbe] = None,
    tool_effect: Optional[ToolEffectExecutor] = None,
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
    from gc_core.settings import runtime_of, settings_of

    settings = settings_of(prep.settings)

    configured_level = settings.level or "default"
    effective_level_provider = level_provider or (lambda: configured_level)
    model_router = ModelRouter(
        settings,
        level_provider=effective_level_provider,
        breaker=circuit_breaker,
        force_legacy=force_legacy,
    )
    # Модель, в которую резолвится main (реестр models), а не легаси-поля.
    start_model, start_url = main_model_identity(settings, model_router)
    run_logger.info(
        "agent started: model=%s url=%s mode=%s",
        start_model,
        start_url,
        settings.backend.api_mode,
        extra={
            "event": "agent_start",
            "model": start_model,
            "url": start_url or None,
            "backend_api_mode": settings.backend.api_mode,
        },
    )

    notice = auto_routing_unavailable_notice(prep.settings)
    if notice is not None:
        run_logger.warning(notice.message)

    run_logger.debug(
        "agent_tools",
        extra={
            "event": "agent_tools",
            "tools": sorted(tools_data.keys()),
            "count": len(tools_data),
        },
    )

    _trace_id = trace_id_from_config(prep.settings)
    if _trace_id:
        # Одна явная запись «под какими именами этот ран шлёт trace». Заголовки
        # нигде не логируются, а имя заголовка конфигурируемо, поэтому это
        # единственное место, где аудит видит фактическую привязку.
        run_logger.info(
            "trace_binding",
            extra={
                "event": "trace_binding",
                "trace_id": _trace_id,
                "model_header": trace_header_name(settings, "x_trace_id"),
                "mcp_header": trace_header_name(settings, "trace_id"),
            },
        )

    workspace = build_execution_workspace(backend, prep.settings)

    prepared = PreparedRunContext(
        settings=settings,
        runtime=runtime_of(prep.settings),
        run_id=effective_run_id,
        trace_id=_trace_id,
        logging_path=logging_path,
        summary_trace_path=summary_trace_path,
        tools_spec=tools_spec,
        tools_description=tools_description,
        mcp_status=mcp_status,
        registered_tool_names=list(registered_names or []),
        project_gc_dir=str(_resolve_project_gc_dir()),
        workspace_root=workspace.root,
        workspace_shell=workspace.shell,
        workspace_os_name=workspace.os_name,
        workspace_root_writable=workspace.root_writable,
        mcp_session_owner=mcp_session_owner,
        is_tui=bool(is_tui),
    )
    bindings = RunContextBindings(
        auth_connector=auth_connector,
        logger=run_logger,
        tools_data=tools_data,
        environment=environment,
        os_backend=backend,
        skills_registry=skills_registry,
        model_router=model_router,
        on_stream_delta=on_stream_delta,
        on_trace_note=on_trace_note,
        on_messages_evicted=on_messages_evicted,
        tools_cache=tools_cache,
        thread_store=thread_store,
        agent_store=prep.agent_store,
        prompt_loader=prompt_loader,
        state_preparer=state_preparer,
        human_request=human_request,
        guard_probe=guard_probe,
        tool_effect=tool_effect,
    )
    ctx = assemble_run_context(prepared, bindings)
    # Хранилище тредов SDK/воркера с файлами треда держит картинки у себя;
    # без него — по конфигу, как раньше.
    ctx.image_store = image_store_for(ctx, thread_store)
    # ``TrackingFsBackend`` ставится ЗДЕСЬ — в том режиме, которому запись
    # правок включена (``agent_edits.record``: TUI по умолчанию, CLI и сервер —
    # по конфигу). Где запись выключена, обёртки нет вовсе: она зовёт
    # ``git status`` до и после каждой команды, а журнал там никто не смотрит.
    # Живёт обёртка в ``gc_core.agent_edits.tracking``, то есть зависимости
    # ядра от фронтенда тут нет (страж ``tests/test_core_does_not_import_frontends``).
    #
    # Фронтенд свою обёртку ставить НЕ ДОЛЖЕН. Прежде её ставил сам TUI
    # (``gc_tui.diff_tui.tracking.attach_to_context``), и при слиянии веток
    # это уже приводило ко второй обёртке поверх первой — каждая правка
    # уходила в журнал дважды. Точка установки ровно одна, и она здесь.
    #
    # Флаг, а не ``os_backend is None``: после ``resolve_os_backend`` параметр
    # уже не отличает «передали свой» от «собрали дефолтный» (см. докстринг
    # ``backend_was_overridden``), и сравнение с ним делало условие вечно
    # ложным, а обёртку — мёртвым кодом.
    from gc_core.agent_edits.ctx import recording_enabled

    if not prep.backend_was_overridden and recording_enabled(ctx):
        from gc_core.agent_edits.tracking import wrap_local_for_tracking

        wrapped = wrap_local_for_tracking(backend, ctx)
        if wrapped is not backend:
            ctx.os_backend = wrapped
            # Подменяем бэкенд в готовой рабочей области, а не собираем её
            # заново: обёртка делегирует внутрь всё, чем область себя измеряет
            # (`default_cwd`, `isabs`, `is_sandbox`), поэтому корень, оболочка и
            # имя ОС у неё те же. Пересборка стоила бы ВТОРОЙ пробы записи в
            # корень проекта (`resolve_root_writable` → реальная запись на
            # диск) на каждом старте — при том что докстринг области обещает
            # ровно одну.
            ctx.workspace = replace(ctx.workspace, backend=wrapped)

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
    "acreate_run_context",
    "assemble_run_context",
]
