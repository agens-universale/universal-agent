import json
import logging
from collections import OrderedDict
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from gc_core.backends.factory import parse_os_backend_kind
from gc_core.backends.protocol import FsBackend
from gc_core.execution.contracts import (
    ExecutionFailure,
    StateDelta,
    ToolResult,
)
from gc_core.execution.reducers import apply_state_delta, tool_state_view
from gc_core.settings import LoadedSettings, runtime_of, settings_of
from gc_core.tools.arguments_validation import (
    WrongFunctionArgumentsException,
    annotate_output_with_dropped_arguments,
    parse_and_validate_tool_arguments,
)
from gc_core.tools.inject_tool_fields import (
    apply_injected_fields_to_spec,
    resolve_inject_tool_fields,
    strip_injected_tool_args,
)
from gc_core.tools.registry import _apply_stop_tools, get_builtin_tools_data
from gc_core.tools.schema_optional import apply_openai_strict_parameters
from gc_core.tools.skills.registry import SkillsRegistry, get_skills_registry
from gc_core.tools.skills.skills_tools import get_skills_tools_data
from gc_core.tools.subagents.registry import is_subagent_tool
from gc_core.tools.subagents.selection import subagents_enabled

_fallback_logger = logging.getLogger(__name__)
from gc_core.execution.tool_feedback import (
    INTERNAL_TOOL_ERROR as INTERNAL_TOOL_ERROR,
)
from gc_core.execution.tool_feedback import (
    _truncate_for_tool_output as _truncate_for_tool_output,
)
from gc_core.execution.tool_feedback import (
    format_internal_tool_error_output as format_internal_tool_error_output,
)
from gc_core.execution.tool_feedback import (
    format_unknown_tool_output as format_unknown_tool_output,
)
from gc_core.execution.tool_feedback import (
    is_internal_tool_error_output as is_internal_tool_error_output,
)
from gc_core.execution.tool_feedback import (
    is_unknown_tool_output as is_unknown_tool_output,
)

#: Бэкенды, у которых файловая система — не хост.
_REMOTE_OS_BACKENDS = frozenset({"harbor", "docker", "ssh", "exec"})


def _ensure_spec_has_required(spec: Dict[str, Any]) -> None:
    """
    Добавить parameters.required если отсутствует.
    Бэкенд требует обязательное поле required в JSON Schema для object-параметров.
    """
    if not isinstance(spec, dict):
        return
    params = spec.get("parameters")
    if not isinstance(params, dict):
        return
    if params.get("type") != "object":
        return
    if "required" not in params or not isinstance(params["required"], list):
        params["required"] = params.get("required") or []


def _apply_openai_strict_tools_schema(spec: Dict[str, Any]) -> None:
    """Delegate to schema_optional (kept for tests that import this name)."""
    if not isinstance(spec, dict):
        return
    params = spec.get("parameters")
    if isinstance(params, dict):
        # OpenAI strict rejects bare property $ref; inline Pydantic $defs if present.
        from gc_core.tools.connect_mcp.agent_tools import (
            _inline_parameter_schema_refs,
        )

        _inline_parameter_schema_refs(params)
        apply_openai_strict_parameters(params)


SUBAGENT_TOOLS_ALLOW_KEY = "subagent_tools_allow"
SUBAGENT_TOOLS_STOP_KEY = "subagent_tools_stop"


def _warn_unknown_filter_names(
    tools_data: Dict[str, Any], allow, stop, logger: Any
) -> None:
    unknown = sorted(((allow or set()) | stop) - set(tools_data))
    if not unknown:
        return
    # Опечатка в имени тула не должна ронять ран: определение уже прошло
    # проверку обязательных тулов, а остальное — предпочтения оператора.
    subagent_names = [name for name in unknown if name.startswith("subagent_")]
    rest = [name for name in unknown if not name.startswith("subagent_")]
    log = logger or _fallback_logger
    if subagent_names:
        # Глубина всегда 1: ребёнку сабагенты не выдаются в принципе.
        log.warning(
            "subagent_tools_filter_nested_ignored names=%s",
            ", ".join(subagent_names),
            extra={
                "event": "subagent_tools_filter_nested_ignored",
                "names": subagent_names,
            },
        )
    if rest:
        log.warning(
            "subagent_tools_filter_unknown names=%s",
            ", ".join(rest),
            extra={"event": "subagent_tools_filter_unknown", "names": rest},
        )


def apply_subagent_tools_filter(
    tools_data: Dict[str, Dict[str, Any]],
    settings: Any,
    *,
    logger: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """Сузить набор тулов дочернего рана по allow/stop из определения сабагента.

    ``settings`` — всё, что принимает ``runtime_of`` (контекст, ``LoadedSettings``):
    allow/stop живут в рантайме ребёнка.

    Точка применения — здесь, а не в раннере: детские тулы собираются в двух
    местах (``_build_child_state`` и ``_build_child_ctx``), оба зовут
    ``build_tools_data``. Фильтр в раннере пришлось бы дублировать, и два
    call-site'а обязаны были бы оставаться синхронными.

    Применяется ПОСЛЕ подмешивания скиллов и MCP, ПОСЛЕ глобального
    ``stop_tools`` по слитому набору и ДО сборки ``tools_spec`` — иначе модель
    увидела бы в схеме тул, который не сможет вызвать. Глобальный stop идёт
    раньше: служебное ядро возвращается только если оно ещё есть во входящем
    наборе, снятое им не восстанавливаем.
    """
    runtime = runtime_of(settings)
    allow_raw = runtime.subagent_tools_allow
    stop_raw = runtime.subagent_tools_stop
    allow = set(allow_raw) if allow_raw is not None else None
    stop = set(stop_raw or ())
    if allow is None and not stop:
        return tools_data

    from gc_core.tools.subagents.custom.file_subagent import (
        REQUIRED_TOOL_NAMES,
    )

    _warn_unknown_filter_names(tools_data, allow, stop, logger)

    filtered = {
        name: entry
        for name, entry in tools_data.items()
        if (allow is None or name in allow) and name not in stop
    }
    # Служебное ядро возвращается безусловно: перечислять его в `tools` не нужно,
    # и `stop_tools` его не отбирает (попытка запрета логируется в реестре). Если
    # ядра нет в исходном наборе — его убрал глобальный stop_tools или harbor, и
    # восстанавливать несуществующее нечего.
    for name in REQUIRED_TOOL_NAMES:
        if name in tools_data:
            filtered[name] = tools_data[name]
    return filtered


def _collect_local_tools_data(
    settings: Any,
    *,
    os_backend: FsBackend | None,
    skills_registry: SkillsRegistry | None,
) -> Dict[str, Dict[str, Any]]:
    """Тулы, которые не требуют сети: builtin и скиллы.

    Отдельно от MCP, потому что дискавери — единственный дорогой источник
    (866–1405 мс против 2.4–6.9 мс на всё это), и только она бывает
    асинхронной. Обе формы ``build_tools_data`` зовут этот сборщик как есть.
    """
    tools_data: Dict[str, Dict[str, Any]] = {}

    builtin_tools_data = get_builtin_tools_data(settings)
    for k, v in builtin_tools_data.items():
        tools_data[k] = v

    registry = skills_registry or get_skills_registry(
        settings, backend=os_backend
    )
    skills_tools_data = get_skills_tools_data(registry)
    for k, v in skills_tools_data.items():
        tools_data[k] = v
    return tools_data


async def abuild_tools_data(
    settings: Any,
    on_progress=None,
    *,
    os_backend: FsBackend | None = None,
    skills_registry: SkillsRegistry | None = None,
    logger: Any = None,
    tools_cache: Any = None,
    registered_names: Optional[list] = None,
) -> Tuple[Dict[str, Dict[str, Any]], list, list, dict]:
    """То же, что ``build_tools_data``, но дискавери ждётся без потока.

    Дискавери — 99.7% времени сборки (866–1405 мс против 2.4–6.9 мс на всё
    остальное), и только она здесь асинхронна. Остальные источники — builtin,
    скиллы, субагенты — это словари в памяти.

    ``settings`` — настройки рана: всё, что принимает ``settings_of``/``runtime_of``
    (контекст, ``LoadedSettings``, ``AppConfig``).
    """
    from gc_core.tools.connect_mcp.agent_tools import (
        aget_mcp_tools_data_for_config,
    )

    tools_data = _collect_local_tools_data(
        settings, os_backend=os_backend, skills_registry=skills_registry
    )
    # `os_backend` обязателен: без него `runs_in_agent_runtime` считает любой
    # MCP-сервер «своим», и `execute_on: os_backend` молча исполнялся на хосте,
    # мимо песочницы. develop передаёт его здесь же; при переводе аргумент
    # потерялся.
    mcp_tools_data, mcp_status = await aget_mcp_tools_data_for_config(
        settings,
        on_progress,
        tools_cache=tools_cache,
        os_backend=os_backend,
    )
    return _finish_tools_data(
        tools_data,
        mcp_tools_data,
        mcp_status,
        settings,
        logger=logger,
        registered_names=registered_names,
    )


def _normalize_tool_spec(
    spec: Dict[str, Any],
    entry: Dict[str, Any],
    *,
    inject_tool_fields,
) -> None:
    """Канон: семантический ``required`` и ``strict`` исходной спеки.

    ``openai_strict_tools_schema`` и ``send_tool_strict`` канон не мутируют.
    Подсказки необязательных полей и полный ``required`` появляются на копии
    в момент отправки (``project_tool_specs``).
    """
    _ensure_spec_has_required(spec)
    if inject_tool_fields:
        injected = apply_injected_fields_to_spec(spec, inject_tool_fields)
        entry["injected_fields"].extend(injected)


def _finish_tools_data(
    tools_data: Dict[str, Dict[str, Any]],
    mcp_tools_data: Dict[str, Dict[str, Any]],
    mcp_status: dict,
    settings: Any,
    *,
    logger: Any,
    registered_names: Optional[list] = None,
) -> Tuple[Dict[str, Dict[str, Any]], list, list, dict]:
    """Досборка после дискавери: субагенты, фильтры, спеки. Ни сети, ни ожидания.

    Одно тело на обе формы вызова — синхронную и асинхронную. Между ними
    отличается ровно то, как дождаться MCP.
    """
    for k, v in mcp_tools_data.items():
        tools_data[k] = v

    # Настройки — один раз и до реестра субагентов: фильтры и спеки ниже
    # читают их отсюда.
    loaded = LoadedSettings(
        settings=settings_of(settings), runtime=runtime_of(settings)
    )
    app = loaded.settings
    backend_mode = app.backend.api_mode.strip().lower()
    if (
        subagents_enabled(app.agent.use_local_subagents)
        and backend_mode == "chat_completions"
    ):
        from gc_core.tools.subagents.registry import get_subagents_tools_data

        # Реестр уже применил список из use_local_subagents к кастомным;
        # встроенные семь доступны всегда, пока фича включена.
        subagent_tools = get_subagents_tools_data(settings, logger=logger)
        for k, v in subagent_tools.items():
            tools_data[k] = v

    if parse_os_backend_kind(loaded) in _REMOTE_OS_BACKENDS:
        # Host-only: пишет на локальный диск. Когда файлы живут «там» (harbor,
        # docker, ssh, exec), на хосте им не место — тул снимается.
        tools_data.pop("download_file", None)

    # Каталог до вычитания: валидатор stop_tools сверяется с ним, а не с уже
    # отфильтрованным словарём (снятый skills_list иначе снова «опечатка»).
    if registered_names is not None:
        registered_names[:] = list(tools_data)

    tools_data = _apply_stop_tools(tools_data, loaded)
    tools_data = apply_subagent_tools_filter(tools_data, loaded, logger=logger)

    # Логирование оставляем модульным, так как build_tools_data вызывается
    # при создании RunContext, когда logger ещё не привязан к контексту.
    _fallback_logger.debug("Collected tools_data: %s", tools_data)

    inject_tool_fields = resolve_inject_tool_fields(loaded)

    # Бэкенд требует parameters.required — нормализуем все spec'и.
    # Флаги схемы на канон не накладываются: проекция — на отправке.
    for v in tools_data.values():
        v.setdefault("injected_fields", [])
        spec = v.get("spec")
        if isinstance(spec, dict):
            _normalize_tool_spec(
                spec,
                v,
                inject_tool_fields=inject_tool_fields,
            )

    tools_spec = [v["spec"] for v in tools_data.values()]
    tools_description = [v["description"] for v in tools_data.values()]

    return tools_data, tools_spec, tools_description, mcp_status


class _SubagentTool:
    """Вызов субагента, отложенный до диспетчера.

    Раньше подготовка вызова исполняла субагента прямо в себе. Пока всё было
    синхронным, это было незаметно; теперь подготовка общая для sync- и
    async-пути и не может решать за них, как ждать долгий вложенный прогон.

    ``acall`` — быстрый путь для async-диспетчера (субагент идёт на том же
    loop). ``__call__`` — синхронный, для прямых вызовов apply_tool и тестов.
    """

    def __init__(
        self, runner, *, tool_name, arguments, run_agent_func, parent_call_id
    ):
        self._runner = runner
        self._tool_name = tool_name
        self._arguments = arguments
        self._run_agent_func = run_agent_func
        self._parent_call_id = parent_call_id

    async def acall(self) -> str:
        return await self._runner.arun(
            self._tool_name,
            self._arguments,
            self._run_agent_func,
            parent_call_id=self._parent_call_id,
        )


def tool_arguments_valid(
    tool_name: str,
    arguments: Any,
    tools_data_: Dict[str, Dict[str, Any]],
) -> bool:
    """True, если аргументы прошли бы валидацию apply_tool (те же правила и spec).

    Неизвестный тул или запись без spec не судим (True): решать будет сам
    apply_tool. Исключение ловится здесь же, поэтому результат не зависит от
    того, какое поколение модуля держит вызывающая сторона.
    """
    entry = tools_data_.get(tool_name) or {}
    spec = entry.get("spec")
    if not isinstance(spec, dict):
        return True
    try:
        parse_and_validate_tool_arguments(
            tool_name=tool_name,
            arguments=arguments,
            tool_spec=spec,
            allow_empty_for_empty_schema=True,
            tool_fabric=entry.get("tool_fabric"),
        )
    except WrongFunctionArgumentsException:
        return False
    return True


def _settings_source_for_call(
    ctx: Optional[Any], kwargs: Dict[str, Any]
) -> Any:
    """Источник настроек вызова (то, что принимает ``settings_of``).

    Контекст рана; явные ``settings=`` вызова — только когда контекста нет или
    у него нет своих настроек (прямые вызовы ``apply_tool`` в тестах/утилитах).
    """
    explicit = kwargs.get("settings")
    if ctx is None:
        return explicit
    if explicit is not None and getattr(ctx, "settings", None) is None:
        return explicit
    return ctx


def _logger_for_call(
    ctx: Optional[Any],
) -> logging.Logger | logging.LoggerAdapter:
    if ctx is not None and hasattr(ctx, "logger"):
        candidate = ctx.logger
        if isinstance(candidate, (logging.Logger, logging.LoggerAdapter)):
            return candidate
    return _fallback_logger


def _prepare_subagent_call(
    tool_name: str,
    arguments: str | Dict[str, Any],
    tools_data_: Dict[str, Dict[str, Any]],
    ctx: Optional[Any],
    settings: Any,
    kwargs: Dict[str, Any],
) -> "tuple[Any, Dict[str, Any], list[str]] | str":
    from gc_core.tools.subagents.runner import SubagentRunner

    orchestrator_state = kwargs.get("state")
    run_agent_func = kwargs.get("run_agent_func")
    if orchestrator_state is None or run_agent_func is None:
        return f"Subagent {tool_name}: missing state or run_agent_func"
    entry = tools_data_.get(tool_name) or {}
    tool_spec = entry.get("spec", {}) or {}
    dropped_unknown: list[str] = []
    parsed_args = parse_and_validate_tool_arguments(
        tool_name=tool_name,
        arguments=arguments,
        tool_spec=tool_spec,
        allow_empty_for_empty_schema=False,
        tool_fabric=entry.get("tool_fabric"),
        dropped_unknown=dropped_unknown,
    )
    prepare = getattr(
        entry.get("tool_fabric"), "prepare_args_before_strip", None
    )
    if callable(prepare):
        parsed_args = prepare(parsed_args)
    parsed_args = strip_injected_tool_args(
        parsed_args,
        injected_fields=entry.get("injected_fields") or [],
    )
    runner = SubagentRunner(ctx, orchestrator_state, settings)
    return (
        _SubagentTool(
            runner,
            tool_name=tool_name,
            arguments=parsed_args,
            run_agent_func=run_agent_func,
            parent_call_id=kwargs.get("call_id"),
        ),
        {},
        dropped_unknown,
    )


def _prepare_tool_call(
    tool_name: str,
    arguments: str | Dict[str, Any],
    tools_data_: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Any] = None,
    **kwargs: Any,
) -> "tuple[Any, Dict[str, Any], list[str]] | str":
    """
    Подготовить вызов тула: вернуть ``(тул, аргументы)`` либо готовую строку-ответ.

    Строка — это ранний выход, к самому вызову не ведущий: неизвестный тул,
    серверный сабагент, результат локального SubagentRunner. Общая часть для
    синхронного ``apply_tool`` и асинхронного ``aapply_tool``: валидация,
    strip инжектируемых полей и сборка фабрики у них одна и та же, различается
    ТОЛЬКО способ позвать сам тул.

    - tools_data_ — явный словарь tools_data (обычный путь в тестах/утилитах);
    - ctx — RunContext; если передан и tools_data_ не задан, берём tools_data из ctx.
      Также из настроек ctx читаются use_server_subagents и server_tool_prefix.
    """
    if tools_data_ is None:
        if ctx is None or not hasattr(ctx, "tools_data"):
            raise RuntimeError(
                "apply_tool requires tools_data_ or ctx with tools_data"
            )
        tools_data_ = ctx.tools_data

    # Настройки server subagents
    source = _settings_source_for_call(ctx, kwargs)
    settings = settings_of(source)
    use_server_subagents = settings.agent.use_server_subagents
    server_tool_prefix = settings.agent.server_tool_prefix

    if (
        use_server_subagents
        and server_tool_prefix
        and tool_name.startswith(server_tool_prefix)
    ):
        return f"{tool_name} call"

    local_subagents_cfg = settings.agent.use_local_subagents
    # Отдельная сверка со списком не нужна: реестр уже отдаёт только разрешённых
    # кастомных, поэтому is_subagent_tool на отфильтрованном вернёт False.
    if subagents_enabled(local_subagents_cfg) and is_subagent_tool(
        tool_name, source
    ):
        return _prepare_subagent_call(
            tool_name, arguments, tools_data_, ctx, source, kwargs
        )

    # Определяем, каким логгером пользоваться: контекстный или модульный.
    log = _logger_for_call(ctx)

    try:
        tool_fabric = tools_data_[tool_name]["tool_fabric"]
    except KeyError:
        log.error(
            "apply_tool: no tool '%s'. kwargs=%s, arguments=%s",
            tool_name,
            kwargs,
            arguments,
        )
        return format_unknown_tool_output(tool_name, tools_data_.keys())

    # Определяем, ожидает ли инструмент пустую схему (нет параметров)
    spec = tools_data_[tool_name].get("spec", {}) or {}
    dropped_unknown: list[str] = []
    arguments = parse_and_validate_tool_arguments(
        tool_name=tool_name,
        arguments=arguments,
        tool_spec=spec,
        allow_empty_for_empty_schema=True,
        tool_fabric=tool_fabric,
        dropped_unknown=dropped_unknown,
    )
    entry = tools_data_[tool_name]
    prepare = getattr(tool_fabric, "prepare_args_before_strip", None)
    if callable(prepare):
        arguments = prepare(arguments)
    arguments = strip_injected_tool_args(
        arguments,
        injected_fields=entry.get("injected_fields") or [],
    )

    # Пробрасываем ctx в kwargs тула: AbstractSimpleTool.__init__ читает его
    # через kwargs.get(...), и тул вместе с security‑декораторами (root_box,
    # read_guard, url_policy, command_moderation) берёт настройки рана из
    # self._ctx. Явные ``settings=`` вызова (если были) уходят туда же.
    fabric_kwargs = dict(kwargs)
    fabric_kwargs.setdefault("ctx", ctx)
    tool = tool_fabric(**fabric_kwargs)
    return tool, arguments, dropped_unknown


def _tool_failure_output(
    exc: BaseException,
    *,
    tool_name: str,
    ctx: Optional[Any],
    call_id: Any,
) -> str:
    """Общий разбор упавшего тула для обоих входов.

    Стек остаётся в логах, модель получает слим-конверт (контракт
    docs_md/actual/2026-08-14-apply-tool-internal-error-CONTRACT.ru.md).

    Один хелпер, а не копия на каждый вход: асинхронный путь отдавал модели
    `str(exc)` вместе с полным traceback — доперезаписанная форма, которую
    develop заменил конвертом. Расхождение было незаметным: тул падал, агент
    получал текст, всё «работало» — только контракт в синхронном и
    асинхронном вызове был разный, а traceback уезжал в контекст модели.
    """
    log = _fallback_logger
    if ctx is not None and hasattr(ctx, "logger"):
        candidate = ctx.logger
        if isinstance(candidate, (logging.Logger, logging.LoggerAdapter)):
            log = candidate

    if not isinstance(call_id, str):
        call_id = None

    log.exception(
        "apply_tool internal error tool=%s call_id=%s",
        tool_name,
        call_id,
    )
    return format_internal_tool_error_output(
        tool_name=tool_name,
        exc=exc,
        call_id=call_id,
    )


def _tool_result_to_text(res: Any) -> str:
    if isinstance(res, str):
        return res
    if isinstance(res, (list, dict)):
        return json.dumps(res, ensure_ascii=False)
    return str(res)


async def _aapply_tool_text(
    tool_name: str,
    arguments: str | Dict[str, Any],
    tools_data_: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Any] = None,
    **kwargs: Any,
) -> str:
    """
    Вызвать тул по имени. Форма у тула ОДНА — ``acall``.

    Раньше здесь было три ступени: ``acall``, корутинный ``__call__`` и, как
    запасной путь, ``asyncio.to_thread`` для синхронного тула. Третья ступень
    существовала «ради встраивающих», но зарегистрировать свой Python-класс-тул
    извне нельзя вовсе: ``build_tools_data`` собирает набор из настроек,
    параметра для чужих тулов у ``create_run_context`` нет, механизма плагинов
    нет. Точка расширения — MCP, и она асинхронна насквозь.

    Асинхронность формы не означает, что тело что-то ждёт: файловые и короткие
    тулы исполняют его инлайн на loop (см. ``test_file_tools_run_inline``).
    """
    prepared = _prepare_tool_call(
        tool_name, arguments, tools_data_, ctx, **kwargs
    )
    if isinstance(prepared, str):
        return prepared
    tool, args, dropped = prepared
    acall = getattr(tool, "acall", None)
    if not callable(acall):
        # Жёстко, а не тихим уходом в поток: тул без `acall` — это ошибка
        # проводки, и она обязана быть видна сразу, а не размазаться по
        # прогону лишним слотом пула.
        raise TypeError(
            f"Tool {tool_name!r} ({type(tool).__name__}) has no acall(); the tool contract is async-only"
        )
    try:
        from gc_core.execution.tool_effects import (
            invoke_native_tool,
            uses_leaf_effect,
        )

        if callable(getattr(ctx, "tool_effect", None)) and uses_leaf_effect(
            tool_name, tool
        ):
            res = await invoke_native_tool(
                tool,
                tool_name,
                args,
                ctx=ctx,
                state=kwargs.get("state"),
                call_id=kwargs.get("call_id"),
                operation_id=kwargs.get("tool_operation_id")
                or str(kwargs.get("call_id") or "tool"),
            )
        else:
            res = await acall(**args)
    except ExecutionFailure:
        raise
    except Exception as e:
        return annotate_output_with_dropped_arguments(
            _tool_failure_output(
                e, tool_name=tool_name, ctx=ctx, call_id=kwargs.get("call_id")
            ),
            dropped,
        )
    # Заметка об отброшенных аргументах — на ОБОИХ выходах, как на develop:
    # модель обязана узнать, что часть переданных полей схема не приняла,
    # независимо от того, отработал тул или упал.
    return annotate_output_with_dropped_arguments(
        _tool_result_to_text(res), dropped
    )


async def aapply_tool_result(
    tool_name: str,
    arguments: str | Dict[str, Any],
    tools_data_: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Any] = None,
    **kwargs: Any,
) -> ToolResult:
    """Run a tool and return its explicitly owned state changes.

    Stateful session tools receive a small private view, not the live State.
    Other legacy tools keep their local contract; durable runtimes must opt in
    only tools whose external effects and required state they can represent.
    """
    state = kwargs.get("state")
    before = None
    view = None
    if (
        tool_name in ("session_memory", "session_todo", "exec_fetch_url")
        and state is not None
    ):
        before = tool_state_view(state)
        view = SimpleNamespace(
            shared=SimpleNamespace(
                working_memory=deepcopy(before.working_memory),
                fetch_url_cache=OrderedDict(deepcopy(before.fetch_url_cache)),
            ),
            session_todo=deepcopy(before.session_todo),
        )
        kwargs = {**kwargs, "state": view}
    output = await _aapply_tool_text(
        tool_name, arguments, tools_data_, ctx, **kwargs
    )
    delta = StateDelta()
    if view is not None:
        if view.shared.working_memory != before.working_memory:
            delta.working_memory = deepcopy(view.shared.working_memory)
        if view.session_todo != before.session_todo:
            delta.session_todo = deepcopy(view.session_todo)
        cache = [
            [key, deepcopy(value)]
            for key, value in view.shared.fetch_url_cache.items()
        ]
        if cache != before.fetch_url_cache:
            delta.fetch_url_cache = cache
    from gc_core.tools.subagents.progress import parse_tool_result_status

    ok, _ = parse_tool_result_status(output)
    return ToolResult(
        output=output,
        outcome="tool_error" if ok is False else "ok",
        delta=delta,
    )


async def aapply_tool(
    tool_name: str,
    arguments: str | Dict[str, Any],
    tools_data_: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Any] = None,
    **kwargs: Any,
) -> str:
    """Compatible string API; the runner applies explicit state deltas once."""
    result = await aapply_tool_result(
        tool_name, arguments, tools_data_, ctx, **kwargs
    )
    state = kwargs.get("state")
    if state is not None:
        apply_state_delta(state, result.delta)
    return result.output


__all__ = [
    "INTERNAL_TOOL_ERROR",
    "WrongFunctionArgumentsException",
    "aapply_tool",
    "aapply_tool_result",
    "format_internal_tool_error_output",
    "format_unknown_tool_output",
    "is_internal_tool_error_output",
    "is_unknown_tool_output",
    "tool_arguments_valid",
]
