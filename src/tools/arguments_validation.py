import json
from typing import Any, Dict

from tools.connect_mcp.validation import _validate_tool_arguments


class WrongFunctionArgumentsException(Exception):
    def __init__(
        self,
        message: str,
        received_args: str,
        expected_schema: dict | None = None,
        tool_name: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.received_args = received_args
        self.expected_schema = expected_schema
        self.tool_name = tool_name

    def to_llm_feedback(self) -> str:
        """
        Формирует четкое и структурированное сообщение об ошибке для LLM.
        """
        feedback = "❌ **Tool Argument Validation Failed**\n\n"

        if self.tool_name:
            feedback += f"**Tool:** `{self.tool_name}`\n\n"
        feedback += f"**Error:** {self.message}\n\n"

        args_display = self.received_args if self.received_args else "(empty or missing)"
        feedback += f"**Received Arguments:**\n```json\n{args_display}\n```\n\n"

        if self.expected_schema:
            feedback += f"**Expected Schema:**\n```json\n{self.expected_schema}\n```\n\n"

        feedback += (
            "**Action Required:**\n"
            "1. Review the error message and schema above.\n"
            "2. Correct the JSON arguments to match the expected schema.\n"
            "3. Ensure all required fields are present and have the correct types.\n"
            "4. Retry the tool call with the fixed arguments."
        )
        return feedback


def parse_and_validate_tool_arguments(
    *,
    tool_name: str,
    arguments: str | Dict[str, Any] | None,
    tool_spec: Dict[str, Any],
    allow_empty_for_empty_schema: bool,
) -> Dict[str, Any]:
    params_schema = tool_spec.get("parameters", {}) if isinstance(tool_spec, dict) else {}
    params_schema = params_schema if isinstance(params_schema, dict) else {}
    properties = params_schema.get("properties", {}) or {}
    required = params_schema.get("required", []) or []
    is_empty_schema = not properties and not required

    if isinstance(arguments, dict):
        parsed_args = arguments
    else:
        arg_str = "" if arguments is None else str(arguments).strip()
        if arg_str == "":
            if allow_empty_for_empty_schema and is_empty_schema:
                parsed_args = {}
            else:
                raise WrongFunctionArgumentsException(
                    message="аргументы инструмента пустые, ожидается валидный JSON-объект",
                    received_args=str(arguments),
                    expected_schema=tool_spec,
                    tool_name=tool_name,
                )
        else:
            try:
                parsed_args = json.loads(arg_str)
            except Exception as e:  # noqa: BLE001
                raise WrongFunctionArgumentsException(
                    message=f"некорректный JSON: {e}",
                    received_args=str(arguments),
                    expected_schema=tool_spec,
                    tool_name=tool_name,
                )

    if not isinstance(parsed_args, dict):
        raise WrongFunctionArgumentsException(
            message="аргументы инструмента должны быть JSON-объектом",
            received_args=str(arguments),
            expected_schema=tool_spec,
            tool_name=tool_name,
        )

    try:
        _validate_tool_arguments(tool_name, params_schema, parsed_args)
    except Exception as e:  # noqa: BLE001
        raise WrongFunctionArgumentsException(
            message=str(e),
            received_args=json.dumps(parsed_args, ensure_ascii=False),
            expected_schema=tool_spec,
            tool_name=tool_name,
        )

    return parsed_args


__all__ = [
    "WrongFunctionArgumentsException",
    "parse_and_validate_tool_arguments",
]
