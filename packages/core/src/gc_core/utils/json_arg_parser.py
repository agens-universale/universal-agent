import json
from typing import Any, Dict


class JsonArgParseError(ValueError):
    def __init__(self, *, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def parse_json_object_arg(value: Any, *, field: str) -> Dict[str, Any]:
    if not isinstance(value, str):
        raise JsonArgParseError(
            field=field, message=f"{field} must be a string"
        )

    raw = value.strip()
    if not raw:
        raise JsonArgParseError(
            field=field, message=f"{field} cannot be empty"
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JsonArgParseError(
            field=field, message=f"{field} must be valid JSON: {exc.msg}"
        ) from exc

    if not isinstance(parsed, dict):
        raise JsonArgParseError(
            field=field, message=f"{field} must be a JSON object"
        )

    return parsed
