from typing import Any, Dict, List

from jsonschema import Draft7Validator


def format_mcp_tool_result(data: dict):
    try:
        if "content" in data and "isError" in data:
            return {
                "content": data["content"],
                "isError": data["isError"],
            }
        return data
    except Exception:
        return data


def _validate_tool_arguments(
    tool_name: str,
    tool_schema: Dict[str, Any],
    arguments: Dict[str, Any],
) -> None:
    """
    Провалидировать аргументы тулы по её JSON Schema.
    Бросает исключение с подробным сообщением, если что‑то не валидно.
    """
    try:
        validator = Draft7Validator(tool_schema)
    except Exception as e:
        # Невалидная сама схема инструмента
        raise ValueError(
            f"Invalid JSON Schema for tool '{tool_name}': {e}"
        ) from e

    errors = sorted(validator.iter_errors(arguments), key=lambda e: e.path)
    if not errors:
        return

    parts: List[str] = []
    for err in errors:
        path = "/".join(str(p) for p in err.path) or "<root>"
        parts.append(f"path='{path}': {err.message}")

    message = (
        f"Arguments for tool '{tool_name}' do not match its schema. "
        f"Details:\n" + "\n".join(parts)
    )
    raise ValueError(message)


__all__ = [
    "_validate_tool_arguments",
    "format_mcp_tool_result",
]
