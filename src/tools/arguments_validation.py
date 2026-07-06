import json
from typing import Any, Dict, Type

from jsonschema import Draft7Validator

from tools.connect_mcp.validation import _validate_tool_arguments
from tools.schema_optional import (
    normalize_optional_empty_as_omit,
    resolve_optional_property_keys,
)


def resolve_tool_arguments_feedback_hint(
    tool_fabric: Type[Any] | None,
    *,
    received_args: str,
    parsed_args: dict[str, Any] | None,
    message: str,
) -> str | None:
    """Delegate optional hint to tool class when it defines wrong_arguments_feedback_hint."""
    if tool_fabric is None:
        return None
    hook = getattr(tool_fabric, "wrong_arguments_feedback_hint", None)
    if not callable(hook):
        return None
    try:
        hint = hook(
            received_args=received_args,
            parsed_args=parsed_args,
            _message=message,
        )
    except Exception:  # noqa: BLE001
        return None
    if hint is None:
        return None
    text = str(hint).strip()
    return text if text else None


def _describe_schema_type(spec: Dict[str, Any]) -> str:
    """Человекочитаемый тип поля для 'Correct call format'.
    Раскрывает anyOf/oneOf (object|array|...), а не наивный <any>."""
    if not isinstance(spec, dict):
        return "any"
    t = spec.get("type")
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "|".join(str(x) for x in t if x != "null") or "any"
    for key in ("anyOf", "oneOf"):
        branches = spec.get(key)
        if isinstance(branches, list):
            types: list[str] = []
            for b in branches:
                if isinstance(b, dict):
                    bt = b.get("type")
                    if isinstance(bt, str) and bt != "null" and bt not in types:
                        types.append(bt)
            if types:
                return "|".join(types)
    return "any"


class WrongFunctionArgumentsException(Exception):
    def __init__(
        self,
        message: str,
        received_args: str,
        expected_schema: dict | None = None,
        tool_name: str | None = None,
        extra_hint: str | None = None,
        call_id: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.received_args = received_args
        self.expected_schema = expected_schema
        self.tool_name = tool_name
        self.extra_hint = extra_hint
        self.call_id = call_id

    def to_llm_feedback(self) -> str:
        """
        Формирует четкое и структурированное сообщение об ошибке для LLM.
        """
        tool_label = f"`{self.tool_name}`" if self.tool_name else "tool"
        feedback = f"❌ Wrong arguments for {tool_label}: {self.message}\n\n"

        args_display = self.received_args if self.received_args else "(empty)"
        feedback += f"You sent: `{args_display}`\n\n"

        if self.extra_hint:
            hint = str(self.extra_hint).strip()
            feedback += hint if hint.endswith("\n") else f"{hint}\n"
            if not feedback.endswith("\n\n"):
                feedback += "\n"

        if self.expected_schema:
            params = self.expected_schema.get("parameters", {}) if isinstance(self.expected_schema, dict) else {}
            properties = params.get("properties", {}) or {}
            required_fields = params.get("required", []) or []
            optional_keys = set(resolve_optional_property_keys(params))
            required_entries = []
            optional_entries = []
            for field, spec in properties.items():
                ftype = _describe_schema_type(spec)
                fdesc = spec.get("description", "")
                fdesc_short = fdesc.split(".")[0] if fdesc else ""
                entry = f'  "{field}": <{ftype}>'
                if fdesc_short:
                    entry += f"  // {fdesc_short}"
                if field in optional_keys:
                    optional_entries.append(entry)
                elif field in required_fields:
                    required_entries.append(entry)
                else:
                    optional_entries.append(entry)
            if required_entries or optional_entries:
                all_field_entries = required_entries + optional_entries
                all_field_lines = []
                if required_entries:
                    all_field_lines.append("  // REQUIRED")
                    all_field_lines.extend(required_entries)
                if optional_entries:
                    all_field_lines.append("  // OPTIONAL")
                    all_field_lines.extend(optional_entries)
                # Add comma after each field entry (non-comment lines), except the last one
                body_lines = []
                last_field_idx = max(
                    i for i, line in enumerate(all_field_lines) if not line.strip().startswith("//")
                )
                for i, line in enumerate(all_field_lines):
                    is_comment = line.strip().startswith("//")
                    is_last_field = (not is_comment and i == last_field_idx)
                    if is_comment or is_last_field:
                        body_lines.append(line)
                    else:
                        body_lines.append(line + ",")
                body = "\n".join(body_lines)
                feedback += f"Correct call format:\n```json\n{{\n{body}\n}}\n```\n\n"

        feedback += "Fix the arguments and retry the tool call."
        return feedback


def _raise_wrong_function_arguments(
    *,
    message: str,
    received_args: str,
    expected_schema: dict | None,
    tool_name: str,
    tool_fabric: Type[Any] | None,
    parsed_args: dict[str, Any] | None,
) -> None:
    raise WrongFunctionArgumentsException(
        message=message,
        received_args=received_args,
        expected_schema=expected_schema,
        tool_name=tool_name,
        extra_hint=resolve_tool_arguments_feedback_hint(
            tool_fabric,
            received_args=received_args,
            parsed_args=parsed_args,
            message=message,
        ),
    )


def _coerce_double_encoded_fields(
    parsed_args: Dict[str, Any], params_schema: Dict[str, Any]
) -> tuple[Dict[str, Any], bool]:
    """Recovery-проход: развернуть поля, пришедшие JSON-строкой второго уровня.

    Для каждого строкового значения, которое не проходит свою под-схему, пробуем
    json.loads; принимаем только dict/list, который под-схему проходит. Строки,
    валидные как есть, не трогаем (поэтому "123" не превращается в 42). Возвращаем
    (изменённая_копия_или_исходник, changed)."""
    properties = params_schema.get("properties", {}) or {}
    if not isinstance(parsed_args, dict) or not properties:
        return parsed_args, False
    coerced = dict(parsed_args)
    changed = False
    for field, value in parsed_args.items():
        if not isinstance(value, str):
            continue
        subschema = properties.get(field)
        if not isinstance(subschema, dict):
            continue
        if Draft7Validator(subschema).is_valid(value):
            continue  # строка валидна как есть — не трогаем
        try:
            decoded = json.loads(value)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(decoded, (dict, list)):
            continue
        if Draft7Validator(subschema).is_valid(decoded):
            coerced[field] = decoded
            changed = True
    return coerced, changed


def parse_and_validate_tool_arguments(
    *,
    tool_name: str,
    arguments: str | Dict[str, Any] | None,
    tool_spec: Dict[str, Any],
    allow_empty_for_empty_schema: bool,
    tool_fabric: Type[Any] | None = None,
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
                _raise_wrong_function_arguments(
                    message="аргументы инструмента пустые, ожидается валидный JSON-объект",
                    received_args=str(arguments),
                    expected_schema=tool_spec,
                    tool_name=tool_name,
                    tool_fabric=tool_fabric,
                    parsed_args=None,
                )
        else:
            try:
                parsed_args = json.loads(arg_str)
            except Exception as e:  # noqa: BLE001
                _raise_wrong_function_arguments(
                    message=f"некорректный JSON: {e}",
                    received_args=str(arguments),
                    expected_schema=tool_spec,
                    tool_name=tool_name,
                    tool_fabric=tool_fabric,
                    parsed_args=None,
                )

    if not isinstance(parsed_args, dict):
        _raise_wrong_function_arguments(
            message="аргументы инструмента должны быть JSON-объектом",
            received_args=str(arguments),
            expected_schema=tool_spec,
            tool_name=tool_name,
            tool_fabric=tool_fabric,
            parsed_args=None,
        )

    try:
        _validate_tool_arguments(tool_name, params_schema, parsed_args)
    except Exception as e:  # noqa: BLE001
        coerced, changed = _coerce_double_encoded_fields(parsed_args, params_schema)
        recovered = False
        if changed:
            try:
                _validate_tool_arguments(tool_name, params_schema, coerced)
            except Exception:  # noqa: BLE001
                recovered = False
            else:
                parsed_args = coerced
                recovered = True
        if not recovered:
            _raise_wrong_function_arguments(
                message=str(e),
                received_args=json.dumps(parsed_args, ensure_ascii=False),
                expected_schema=tool_spec,
                tool_name=tool_name,
                tool_fabric=tool_fabric,
                parsed_args=parsed_args,
            )

    return normalize_optional_empty_as_omit(parsed_args, params_schema)


__all__ = [
    "WrongFunctionArgumentsException",
    "parse_and_validate_tool_arguments",
    "resolve_tool_arguments_feedback_hint",
]
