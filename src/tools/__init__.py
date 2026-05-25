import json
import traceback
import sys
import logging
from typing import Any, Dict, Tuple, Optional

from tools.connect_mcp import get_mcp_tools_data
from tools.arguments_validation import (
    WrongFunctionArgumentsException,
    parse_and_validate_tool_arguments,
)
from tools.skills.registry import get_skills_registry
from tools.skills.skills_tools import get_skills_tools_data
from tools.simple_tools import get_simple_tools_data
from tools.subagents.registry import is_subagent_tool

_fallback_logger = logging.getLogger(__name__)


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
    """
    OpenAI-like validators can require object schema to have:
    - explicit required list
    - required covering all properties keys
    """
    if not isinstance(spec, dict):
        return
    params = spec.get("parameters")
    if not isinstance(params, dict) or params.get("type") != "object":
        return
    props = params.get("properties")
    if not isinstance(props, dict) or not props:
        return
    params["required"] = list(props.keys())
    if "additionalProperties" not in params:
        params["additionalProperties"] = False


def build_tools_data(
    config_dict: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, Any]], list, list]:
    """
    Собрать единый словарь tools_data и производные списки spec/description
    на основе config_dict.
    """
    tools_data: Dict[str, Dict[str, Any]] = {}

    simple_tools_data = get_simple_tools_data(config_dict)
    for k, v in simple_tools_data.items():
        tools_data[k] = v

    skills_registry = get_skills_registry(config_dict)
    skills_tools_data = get_skills_tools_data(skills_registry)
    for k, v in skills_tools_data.items():
        tools_data[k] = v

    mcp_tools_data = get_mcp_tools_data(config_dict)
    for k, v in mcp_tools_data.items():
        tools_data[k] = v

    use_local_subagents = config_dict.get("use_local_subagents", False)
    backend_mode = str(config_dict.get("backend_api_mode", "responses")).strip().lower()
    if use_local_subagents and backend_mode == "chat_completions":
        from tools.subagents.registry import get_subagents_tools_data
        subagent_tools = get_subagents_tools_data()
        for k, v in subagent_tools.items():
            tools_data[k] = v

    # Логирование оставляем модульным, так как build_tools_data вызывается
    # при создании RunContext, когда logger ещё не привязан к контексту.
    _fallback_logger.debug("Collected tools_data: %s", tools_data)

    openai_strict = bool(config_dict.get("openai_strict_tools_schema", False))

    # Бэкенд требует parameters.required — нормализуем все spec'и
    for v in tools_data.values():
        spec = v.get("spec")
        if isinstance(spec, dict):
            _ensure_spec_has_required(spec)
            if openai_strict:
                _apply_openai_strict_tools_schema(spec)

    tools_spec = [v["spec"] for v in tools_data.values()]
    tools_description = [v["description"] for v in tools_data.values()]

    return tools_data, tools_spec, tools_description


def apply_tool(
    tool_name: str,
    arguments: str | Dict[str, Any],
    tools_data_: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Any] = None,
    **kwargs: Any,
) -> str:
    """
    Вызвать тул по имени.

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

    use_local_subagents = config_dict.get("use_local_subagents", False)
    if use_local_subagents and is_subagent_tool(tool_name):
        from tools.subagents.runner import SubagentRunner  # lazy: runner imports from tools
        orchestrator_state = kwargs.get("state")
        run_agent_func = kwargs.get("run_agent_func")
        if orchestrator_state is None or run_agent_func is None:
            return f"Subagent {tool_name}: missing state or run_agent_func"
        tool_spec = tools_data_.get(tool_name, {}).get("spec", {}) or {}
        parsed_args = parse_and_validate_tool_arguments(
            tool_name=tool_name,
            arguments=arguments,
            tool_spec=tool_spec,
            allow_empty_for_empty_schema=False,
        )
        runner = SubagentRunner(ctx, orchestrator_state, config_dict)
        return runner.run(tool_name, parsed_args, run_agent_func)

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
        return f"{tool_name} - there is not such tool on client"

    # Определяем, ожидает ли инструмент пустую схему (нет параметров)
    spec = tools_data_[tool_name].get("spec", {}) or {}
    arguments = parse_and_validate_tool_arguments(
        tool_name=tool_name,
        arguments=arguments,
        tool_spec=spec,
        allow_empty_for_empty_schema=True,
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
    # log.debug(
    #    "apply_tool log:\nkwargs:\n%s\narguments:\n %s\ntool_fabric:\n%s\ntool:\n%s",
    #    kwargs,
    #    arguments,
    #    tool_fabric,
    #    tool,
    #)
    try:
        res = tool.__call__(**arguments)
    except Exception as e:  # noqa: BLE001
        res = str(e)
        res += (
            "\n"
            + "Agent tool execution Error in cli agent tool execution: "
            + traceback.format_exc()
        )

    if isinstance(res, str):
        res_str = res
    elif isinstance(res, list):
        res_str = json.dumps(res, ensure_ascii=False)
    elif isinstance(res, dict):
        res_str = json.dumps(res, ensure_ascii=False)
    else:
        res_str = str(res)

    return res_str


__all__ = [
    "WrongFunctionArgumentsException",
    "build_tools_data",
    "apply_tool",
]