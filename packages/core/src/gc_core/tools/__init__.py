import asyncio
import difflib
import inspect
import json
import traceback
import logging
from typing import Any, Dict, Iterable, Tuple, Optional

from gc_core.tools.connect_mcp import get_mcp_tools_data
from gc_core.tools.arguments_validation import WrongFunctionArgumentsException, parse_and_validate_tool_arguments
from gc_core.tools.skills.registry import get_skills_registry
from gc_core.tools.skills.skills_tools import get_skills_tools_data
from gc_core.tools.registry import get_builtin_tools_data
from gc_core.backends.factory import parse_os_backend_kind
from gc_core.backends.protocol import FsBackend
from gc_core.tools.skills.registry import SkillsRegistry
from gc_core.tools.subagents.registry import is_subagent_tool
from gc_core.tools.subagents.selection import subagents_enabled
from gc_core.tools.inject_tool_fields import apply_injected_fields_to_spec, resolve_inject_tool_fields, strip_injected_tool_args
from gc_core.tools.schema_optional import apply_openai_strict_parameters, augment_optional_property_descriptions

_fallback_logger = logging.getLogger(__name__)
_LEGACY_UNKNOWN_TOOL_MARKER = "there is not such tool"


def format_unknown_tool_output(tool_name: str, available_names: Iterable[str]) -> str:
    suggestions = difflib.get_close_matches(
        tool_name, list(available_names), n=5, cutoff=0.3
    )
    error_message = f"No tool named '{tool_name}' on client."
    use_only = (
        "Use only tools from the provided tool list "
        "in section <tools> in system prompt."
    )
    if suggestions:
        message = (
            f"Unknown tool '{tool_name}'. Maybe you mean tools: "
            f"{', '.join(suggestions)}. {use_only}"
        )
    else:
        message = f"Unknown tool '{tool_name}'. {use_only}"
    payload = {
        "ok": False,
        "error": {
            "type": "unknown_tool",
            "message": error_message,
            "requested": tool_name,
            "suggestions": suggestions,
        },
        "message": message,
    }
    return json.dumps(payload, ensure_ascii=False)


def is_unknown_tool_output(output: str) -> bool:
    if not isinstance(output, str) or not output:
        return False
    if _LEGACY_UNKNOWN_TOOL_MARKER in output:
        return True
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    error = parsed.get("error")
    return isinstance(error, dict) and error.get("type") == "unknown_tool"


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
        from gc_core.tools.connect_mcp.agent_tools import _inline_parameter_schema_refs

        _inline_parameter_schema_refs(params)
        apply_openai_strict_parameters(params)


SUBAGENT_TOOLS_ALLOW_KEY = "subagent_tools_allow"
SUBAGENT_TOOLS_STOP_KEY = "subagent_tools_stop"


def apply_subagent_tools_filter(
    tools_data: Dict[str, Dict[str, Any]],
    config_dict: Dict[str, Any],
    *,
    logger: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """Сузить набор тулов дочернего рана по allow/stop из определения сабагента.

    Точка применения — здесь, а не в раннере: детские тулы собираются в двух
    местах (``_build_child_state`` и ``_build_child_ctx``), оба зовут
    ``build_tools_data``. Фильтр в раннере пришлось бы дублировать, и два
    call-site'а обязаны были бы оставаться синхронными.

    Применяется ПОСЛЕ подмешивания скиллов и MCP (иначе ``mcp_*`` было бы не
    отфильтровать, как это происходит с глобальным ``stop_tools``) и ДО сборки
    ``tools_spec`` — иначе модель увидела бы в схеме тул, который не сможет
    вызвать.
    """
    allow_raw = config_dict.get(SUBAGENT_TOOLS_ALLOW_KEY)
    stop_raw = config_dict.get(SUBAGENT_TOOLS_STOP_KEY)
    allow = set(allow_raw) if allow_raw is not None else None
    stop = set(stop_raw or ())
    if allow is None and not stop:
        return tools_data

    from gc_core.tools.subagents.custom.file_subagent import REQUIRED_TOOL_NAMES

    known = set(tools_data)
    unknown = sorted(((allow or set()) | stop) - known)
    if unknown:
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


def build_tools_data(
    config_dict: Dict[str, Any],
    on_progress=None,
    *,
    os_backend: FsBackend | None = None,
    skills_registry: SkillsRegistry | None = None,
    logger: Any = None,
) -> Tuple[Dict[str, Dict[str, Any]], list, list, dict]:
    """
    Собрать единый словарь tools_data и производные списки spec/description
    на основе config_dict.
    """
    tools_data: Dict[str, Dict[str, Any]] = {}

    builtin_tools_data = get_builtin_tools_data(config_dict)
    for k, v in builtin_tools_data.items():
        tools_data[k] = v

    registry = skills_registry or get_skills_registry(config_dict, backend=os_backend)
    skills_tools_data = get_skills_tools_data(registry)
    for k, v in skills_tools_data.items():
        tools_data[k] = v

    mcp_tools_data, mcp_status = get_mcp_tools_data(config_dict, on_progress=on_progress)
    for k, v in mcp_tools_data.items():
        tools_data[k] = v

    local_subagents_cfg = config_dict.get("use_local_subagents", False)
    backend_mode = str(config_dict.get("backend_api_mode", "responses")).strip().lower()
    if subagents_enabled(local_subagents_cfg) and backend_mode == "chat_completions":
        from gc_core.tools.subagents.registry import get_subagents_tools_data
        # Реестр уже применил список из use_local_subagents к кастомным;
        # встроенные семь доступны всегда, пока фича включена.
        subagent_tools = get_subagents_tools_data(config_dict, logger=logger)
        for k, v in subagent_tools.items():
            tools_data[k] = v

    if parse_os_backend_kind(config_dict) == "harbor":
        # Host-only: writes to local disk; not available in harbor/sandbox mode.
        tools_data.pop("download_file", None)

    tools_data = apply_subagent_tools_filter(tools_data, config_dict, logger=logger)

    # Логирование оставляем модульным, так как build_tools_data вызывается
    # при создании RunContext, когда logger ещё не привязан к контексту.
    _fallback_logger.debug("Collected tools_data: %s", tools_data)

    openai_strict = bool(config_dict.get("openai_strict_tools_schema", False))
    send_tool_strict = bool(config_dict.get("send_tool_strict", True))
    inject_tool_fields = resolve_inject_tool_fields(config_dict)

    # Бэкенд требует parameters.required — нормализуем все spec'и
    for v in tools_data.values():
        v.setdefault("injected_fields", [])
        spec = v.get("spec")
        if isinstance(spec, dict):
            _ensure_spec_has_required(spec)
            if inject_tool_fields:
                injected = apply_injected_fields_to_spec(spec, inject_tool_fields)
                v["injected_fields"].extend(injected)
            if openai_strict:
                _apply_openai_strict_tools_schema(spec)
            augment_optional_property_descriptions(spec, openai_strict=openai_strict)
            if not send_tool_strict:
                spec.pop("strict", None)

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

    def __init__(self, runner, *, tool_name, arguments, run_agent_func, parent_call_id):
        self._runner = runner
        self._tool_name = tool_name
        self._arguments = arguments
        self._run_agent_func = run_agent_func
        self._parent_call_id = parent_call_id

    def __call__(self) -> str:
        return self._runner.run(
            self._tool_name, self._arguments, self._run_agent_func,
            parent_call_id=self._parent_call_id,
        )

    async def acall(self) -> str:
        return await self._runner.arun(
            self._tool_name, self._arguments, self._run_agent_func,
            parent_call_id=self._parent_call_id,
        )


def _prepare_tool_call(
    tool_name: str,
    arguments: str | Dict[str, Any],
    tools_data_: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Any] = None,
    **kwargs: Any,
) -> "tuple[Any, Dict[str, Any]] | str":
    """
    Подготовить вызов тула: вернуть ``(тул, аргументы)`` либо готовую строку-ответ.

    Строка — это ранний выход, к самому вызову не ведущий: неизвестный тул,
    серверный сабагент, результат локального SubagentRunner. Общая часть для
    синхронного ``apply_tool`` и асинхронного ``aapply_tool``: валидация,
    strip инжектируемых полей и сборка фабрики у них одна и та же, различается
    ТОЛЬКО способ позвать сам тул.

    - tools_data_ — явный словарь tools_data (обычный путь в тестах/утилитах);
    - ctx — RunContext; если передан и tools_data_ не задан, берём tools_data из ctx.
      Также из ctx.config_dict читаются use_server_subagents и server_tool_prefix.
    """
    if tools_data_ is None:
        if ctx is None or not hasattr(ctx, "tools_data"):
            raise RuntimeError("apply_tool requires tools_data_ or ctx with tools_data")
        tools_data_ = ctx.tools_data

    # Настройки server subagents
    use_server_subagents = False
    server_tool_prefix: Optional[str] = None
    config_dict: Dict[str, Any] = {}

    if ctx is not None and hasattr(ctx, "config_dict"):
        cfg = getattr(ctx, "config_dict") or {}
        if isinstance(cfg, dict):
            config_dict = cfg
    else:
        cfg = kwargs.get("config_dict")
        if isinstance(cfg, dict):
            config_dict = cfg

    use_server_subagents = config_dict.get("use_server_subagents", False)
    server_tool_prefix = config_dict.get("server_tool_prefix")

    if use_server_subagents and server_tool_prefix and tool_name.startswith(
        server_tool_prefix
    ):
        return f"{tool_name} call"

    local_subagents_cfg = config_dict.get("use_local_subagents", False)
    # Отдельная сверка со списком не нужна: реестр уже отдаёт только разрешённых
    # кастомных, поэтому is_subagent_tool на отфильтрованном вернёт False.
    if subagents_enabled(local_subagents_cfg) and is_subagent_tool(
        tool_name, config_dict
    ):
        from gc_core.tools.subagents.runner import SubagentRunner
        orchestrator_state = kwargs.get("state")
        run_agent_func = kwargs.get("run_agent_func")
        if orchestrator_state is None or run_agent_func is None:
            return f"Subagent {tool_name}: missing state or run_agent_func"
        entry = tools_data_.get(tool_name) or {}
        tool_spec = entry.get("spec", {}) or {}
        parsed_args = parse_and_validate_tool_arguments(
            tool_name=tool_name,
            arguments=arguments,
            tool_spec=tool_spec,
            allow_empty_for_empty_schema=False,
            tool_fabric=entry.get("tool_fabric"),
        )
        prepare = getattr(entry.get("tool_fabric"), "prepare_args_before_strip", None)
        if callable(prepare):
            parsed_args = prepare(parsed_args)
        parsed_args = strip_injected_tool_args(
            parsed_args,
            injected_fields=entry.get("injected_fields") or [],
        )
        runner = SubagentRunner(ctx, orchestrator_state, config_dict)
        return _SubagentTool(
            runner,
            tool_name=tool_name,
            arguments=parsed_args,
            run_agent_func=run_agent_func,
            parent_call_id=kwargs.get("call_id"),
        ), {}

    # Определяем, каким логгером пользоваться: контекстный или модульный.
    log = _fallback_logger
    if ctx is not None and hasattr(ctx, "logger"):
        candidate = getattr(ctx, "logger")
        if isinstance(candidate, logging.Logger):
            log = candidate

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
    arguments = parse_and_validate_tool_arguments(
        tool_name=tool_name,
        arguments=arguments,
        tool_spec=spec,
        allow_empty_for_empty_schema=True,
        tool_fabric=tool_fabric,
    )
    entry = tools_data_[tool_name]
    prepare = getattr(tool_fabric, "prepare_args_before_strip", None)
    if callable(prepare):
        arguments = prepare(arguments)
    arguments = strip_injected_tool_args(
        arguments,
        injected_fields=entry.get("injected_fields") or [],
    )

    # Пробрасываем ctx/config_dict в kwargs тула: AbstractSimpleTool.__init__
    # читает их через kwargs.get(...), и security‑декораторы (root_box,
    # read_guard, url_policy, command_moderation) резолвят политику через
    # self._ctx.config_dict. Без этого декораторы получают пустой
    # config_dict и считают security‑слой не сконфигурированным.
    fabric_kwargs = dict(kwargs)
    fabric_kwargs.setdefault("ctx", ctx)
    if config_dict:
        fabric_kwargs.setdefault("config_dict", config_dict)
    tool = tool_fabric(**fabric_kwargs)
    return tool, arguments


def _tool_exception_output(exc: BaseException) -> str:
    """Текст, который агент увидит вместо результата упавшего тула."""
    return (
        str(exc)
        + "\n"
        + "Agent tool execution Error in cli agent tool execution: "
        + traceback.format_exc()
    )


def _tool_result_to_text(res: Any) -> str:
    if isinstance(res, str):
        return res
    if isinstance(res, (list, dict)):
        return json.dumps(res, ensure_ascii=False)
    return str(res)


def apply_tool(
    tool_name: str,
    arguments: str | Dict[str, Any],
    tools_data_: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Any] = None,
    **kwargs: Any,
) -> str:
    """
    Вызвать тул по имени (синхронно).

    Контракт не менялся: та же сигнатура, тот же строковый результат, портала
    на пути нет. Асинхронный тул синхронным путём позвать нельзя — об этом
    говорится прямо, а не возвращается корутина, приведённая к строке.
    """
    prepared = _prepare_tool_call(tool_name, arguments, tools_data_, ctx, **kwargs)
    if isinstance(prepared, str):
        return prepared
    tool, args = prepared
    if inspect.iscoroutinefunction(tool.__call__):
        return (
            f"Tool {tool_name} is async; call it through aapply_tool() "
            f"(sync apply_tool cannot await)."
        )
    try:
        res = tool(**args)
    except Exception as e:  # noqa: BLE001
        res = _tool_exception_output(e)
    return _tool_result_to_text(res)


async def aapply_tool(
    tool_name: str,
    arguments: str | Dict[str, Any],
    tools_data_: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Any] = None,
    **kwargs: Any,
) -> str:
    """
    Вызвать тул по имени (асинхронно), приняв и синхронный, и асинхронный тул.

    Корутину ждём, обычную функцию уводим в поток. Смешение легально навсегда:
    у встраивающих SDK свои тулы, и переезд ядра на asyncio не имеет права
    требовать от них переписывания — синхронный ``__call__`` продолжает
    работать, просто не блокируя loop.
    """
    prepared = _prepare_tool_call(tool_name, arguments, tools_data_, ctx, **kwargs)
    if isinstance(prepared, str):
        return prepared
    tool, args = prepared
    acall = getattr(tool, "acall", None)
    try:
        if callable(acall):
            # Тул объявил асинхронный вход: зовём его, не занимая поток.
            res = await acall(**args)
        elif inspect.iscoroutinefunction(tool.__call__):
            res = await tool(**args)
        else:
            res = await asyncio.to_thread(lambda: tool(**args))
    except Exception as e:  # noqa: BLE001
        res = _tool_exception_output(e)
    return _tool_result_to_text(res)


__all__ = [
    "WrongFunctionArgumentsException",
    "build_tools_data",
    "apply_tool",
    "aapply_tool",
    "format_unknown_tool_output",
    "is_unknown_tool_output",
]