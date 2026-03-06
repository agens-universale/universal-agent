import json
import traceback
import sys
import logging
from typing import Any, Dict, Tuple, Optional

from tools.connect_mcp import get_mcp_tools_data
from tools.skills.registry import get_skills_registry
from tools.skills.skills_tools import get_skills_tools_data
from tools.simple_tools import get_simple_tools_data

_fallback_logger = logging.getLogger(__name__)


class WrongFunctionArgumentsException(Exception):
    def __init__(
        self,
        message: str,
        received_args: str,
        expected_schema: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.received_args = received_args
        self.expected_schema = expected_schema

    def to_llm_feedback(self) -> str:
        """
        Формирует четкое и структурированное сообщение об ошибке для LLM.
        """
        feedback = "❌ **Tool Argument Validation Failed**\n\n"

        # 1. Описание проблемы
        feedback += f"**Error:** {self.message}\n\n"

        # 2. Что было получено (с обработкой пустых значений)
        args_display = self.received_args if self.received_args else "(empty or missing)"
        feedback += f"**Received Arguments:**\n```json\n{args_display}\n```\n\n"

        # 3. Что ожидалось (если есть схема)
        if self.expected_schema:
            feedback += f"**Expected Schema:**\n```json\n{self.expected_schema}\n```\n\n"

        # 4. Четкая инструкция к действию
        feedback += (
            "**Action Required:**\n"
            "1. Review the error message and schema above.\n"
            "2. Correct the JSON arguments to match the expected schema.\n"
            "3. Ensure all required fields are present and have the correct types.\n"
            "4. Retry the tool call with the fixed arguments."
        )

        return feedback


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

    # Логирование оставляем модульным, так как build_tools_data вызывается
    # при создании RunContext, когда logger ещё не привязан к контексту.
    _fallback_logger.debug("Collected tools_data: %s", tools_data)

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
    params_schema = spec.get("parameters", {}) or {}
    properties = params_schema.get("properties", {}) or {}
    required = params_schema.get("required", []) or []
    is_empty_schema = not properties and not required

    # Нормализуем и парсим аргументы
    if isinstance(arguments, dict):
        parsed_arguments = arguments
    else:
        arg_str = "" if arguments is None else str(arguments).strip()

        if arg_str == "":
            # Пустые аргументы:
            # - если схема пустая, подменяем на {} и вызываем инструмент без параметров;
            # - если схема не пустая, считаем это ошибкой и даём LLM подробный фидбек.
            if is_empty_schema:
                parsed_arguments = {}
            else:
                raise WrongFunctionArgumentsException(
                    message="аргументы инструмента пустые, ожидается валидный JSON-объект",
                    received_args=str(arguments),
                    expected_schema=tools_data_[tool_name]["spec"],
                )
        else:
            try:
                parsed_arguments = json.loads(arg_str)
            except Exception as e:  # noqa: BLE001
                raise WrongFunctionArgumentsException(
                    message=f"некорректный JSON: {e}",
                    received_args=str(arguments),
                    expected_schema=tools_data_[tool_name]["spec"],
                )

    arguments = parsed_arguments

    tool = tool_fabric(**kwargs)
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