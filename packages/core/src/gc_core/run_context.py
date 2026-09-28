import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

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
from gc_core.execution_workspace import (
    ExecutionWorkspace,
    aprime_backend_cwd,
    build_execution_workspace,
)
from gc_core.mcp_session_owner import new_owner_token, use_owner
from gc_core.model_router import CircuitBreaker, ModelRouter
from gc_core.tools import abuild_tools_data
from gc_core.tools.skills.registry import SkillsRegistry, get_skills_registry
from gc_core.trace_id import trace_header_name, trace_id_from_config
from gc_core.utils.logging_module import create_run_logger, generate_run_id
from gc_core.utils.prod_auth import AuthConnector


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
    # ToolsCache этого контекста, если владелец его передал. Лежит здесь, чтобы
    # субагенты (они пересобирают tools_data на каждый спавн) брали готовое, а не
    # ходили в дискавери заново.
    tools_cache: Any = None
    # Владелец живых MCP-сессий этого контекста (см. gc_core.mcp_session_owner).
    # По нему ``arun`` понимает, ЧТО ему закрывать на выходе, и не сносит
    # сессии параллельных агентов. Пустая строка — контекст собран в обход
    # create_run_context (тесты, ручная сборка); тогда владельца нет.
    mcp_session_owner: str = ""


async def acreate_run_context(  # noqa: PLR0913  # S107: won't fix, см. CONTRIBUTING.md
    config_dict: Dict[str, Any],
    *,
    auth_connector: Optional[AuthConnector] = None,
    run_id: Optional[str] = None,
    on_progress=None,
    environment: Optional[Any] = None,
    loop: Optional[Any] = None,
    os_backend: Optional[FsBackend] = None,
    on_stream_delta=None,
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
    - ``os_backend`` собирается фабрикой из ``config_dict['os_backend']`` (default: local).
      Для ``harbor`` нужны ``environment`` и ``loop``; тесты могут передать ``os_backend=``.
    - Свой LocalFsBackend → ``TrackingFsBackend`` (журнал правок) во всех
      режимах: правки из CLI/server попадают в журнал треда так же, как из TUI.
    - ``level_provider`` — источник активного уровня для ``model_router`` (спека §6.4).
      CLI/server не передают его → константный провайдер из ключа ``config_dict["level"]``.
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
    if thread_store is not None:
        from gc_core.thread_store_async import athread_store

        athread_store(thread_store)

    prep = _prepare_run_context(
        config_dict,
        run_id=run_id,
        environment=environment,
        loop=loop,
        os_backend=os_backend,
        log_handlers=log_handlers,
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
        await aprime_backend_cwd(prep.backend, config_dict)
        # Каталог имён ДО вычитания stop_tools: валидатор сверяется с ним, иначе
        # успешно снятый тул снова считается опечаткой. Способность пришла с
        # develop, у нас её не было; тело, которое список заполняет, слилось
        # автоматически — без самого списка оно осталось бы мёртвым кодом.
        registered: list[str] = []
        with use_owner(prep.mcp_session_owner):
            tools = await abuild_tools_data(
                config_dict,
                on_progress=on_progress,
                os_backend=prep.backend,
                skills_registry=prep.skills_registry,
                logger=prep.run_logger,
                tools_cache=tools_cache,
                registered_names=registered,
            )
        return _finish_run_context(
            config_dict,
            prep,
            tools,
            registered_names=registered,
            auth_connector=auth_connector,
            environment=environment,
            on_stream_delta=on_stream_delta,
            on_messages_evicted=on_messages_evicted,
            is_tui=is_tui,
            tools_cache=tools_cache,
            thread_store=thread_store,
            level_provider=level_provider,
            circuit_breaker=circuit_breaker,
            force_legacy=force_legacy,
        )
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
    #: Нужен на шаге досборки: TUI оборачивает локальный бэкенд в
    #: ``TrackingFsBackend`` (diff-журнал) только тогда, когда бэкенд собрали
    #: мы сами. Чужой не трогаем — вызывающий знает, что подсунул.
    #:
    #: Флагом, а не самим бэкендом: после ``resolve_os_backend`` отличить
    #: «передали свой» от «собрали дефолтный» уже нечем, и в первой редакции
    #: разделения это привело к тому, что ``os_backend is None`` в досборке
    #: сравнивалось с локальной переменной, всегда не-None. Условие стало
    #: вечно ложным, а обёртка — мёртвым кодом.
    backend_was_overridden: bool


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
        config_dict,
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
    )


def _finish_run_context(  # noqa: PLR0913  # S107: won't fix, см. CONTRIBUTING.md
    config_dict: Dict[str, Any],
    prep: "_RunContextPrep",
    tools: tuple,
    *,
    registered_names: Optional[list] = None,
    auth_connector: Optional[AuthConnector],
    environment: Optional[Any],
    on_stream_delta,
    on_messages_evicted: Optional[Callable[[List[Dict[str, Any]]], None]],
    is_tui: bool,
    tools_cache: Any,
    thread_store: Any = None,
    level_provider: Optional[Callable[[], str]] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    force_legacy: bool = False,
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
        registered_tool_names=set(registered_names or []),
        project_gc_dir=_resolve_project_gc_dir(),
        environment=environment,
        os_backend=backend,
        skills_registry=skills_registry,
        workspace=workspace,
        model_router=model_router,
        on_stream_delta=on_stream_delta,
        on_messages_evicted=on_messages_evicted,
        mcp_session_owner=mcp_session_owner,
        tools_cache=tools_cache,
        thread_store=thread_store,
    )
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
    ctx.is_tui = bool(is_tui)

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
]
