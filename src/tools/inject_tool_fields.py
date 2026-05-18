"""
Инжект синтетических полей в JSON Schema tools из конфига.

Конфиг agent.inject_tool_fields: имя поля → description (строка в JSON Schema).
Если секции нет или dict пустой — инжект не выполняется.
"""
import copy
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def resolve_inject_tool_fields(config_dict: Dict[str, Any]) -> Dict[str, str]:
    """Нормализовать inject_tool_fields из config_dict."""
    raw = config_dict.get("inject_tool_fields")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "inject_tool_fields must be a mapping (field → description), got %s",
            type(raw).__name__,
        )
        return {}

    result: Dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        name = key.strip()
        if not name or name in result:
            continue
        if not isinstance(value, str) or not value.strip():
            logger.warning(
                "inject_tool_fields: skip %r — description must be a non-empty string",
                name,
            )
            continue
        result[name] = value.strip()
    return result


def apply_injected_fields_to_spec(
    spec: Dict[str, Any],
    fields: Dict[str, str],
) -> List[str]:
    """Добавить поля из конфига в spec; вернуть имена реально добавленных полей."""
    injected: List[str] = []
    for field_name, description in fields.items():
        property_schema = {"type": "string", "description": description}
        injected.extend(
            inject_property_into_spec(
                spec,
                field_name=field_name,
                property_schema=property_schema,
            )
        )
    return injected


def inject_property_into_spec(
    spec: Dict[str, Any],
    *,
    field_name: str,
    property_schema: Dict[str, Any],
) -> List[str]:
    """
    Добавить синтетическое поле в JSON Schema tool spec (первым в properties).

    Возвращает [field_name] если поле добавлено, иначе [].
    """
    if not isinstance(spec, dict):
        return []

    params = spec.get("parameters")
    if not isinstance(params, dict):
        spec["parameters"] = _new_object_parameters_with_field(field_name, property_schema)
        return [field_name]

    if _is_combinator_only_parameters(params):
        logger.debug(
            "inject_property_into_spec: skip combinator-only parameters for tool %s field %s",
            spec.get("name"),
            field_name,
        )
        return []

    props = params.get("properties")
    if isinstance(props, dict) and field_name in props:
        return []

    if params.get("type") != "object":
        if not isinstance(props, dict):
            spec["parameters"] = _new_object_parameters_with_field(field_name, property_schema)
            return [field_name]
        params["type"] = "object"

    if not isinstance(props, dict):
        props = {}
        params["properties"] = props

    params["properties"] = {
        field_name: copy.deepcopy(property_schema),
        **props,
    }

    required = params.get("required")
    if not isinstance(required, list):
        required = []
    if field_name not in required:
        params["required"] = [field_name, *required]
    elif required[0] != field_name:
        params["required"] = [
            field_name,
            *[r for r in required if r != field_name],
        ]

    return [field_name]


def strip_injected_tool_args(
    parsed_args: Dict[str, Any],
    *,
    injected_fields: List[str],
) -> Dict[str, Any]:
    """Удалить из parsed_args ключи, добавленные декораторами schema."""
    if not injected_fields:
        return parsed_args
    result = dict(parsed_args)
    for field in injected_fields:
        result.pop(field, None)
    return result


def _new_object_parameters_with_field(
    field_name: str,
    property_schema: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {field_name: copy.deepcopy(property_schema)},
        "required": [field_name],
        "additionalProperties": False,
    }


def _is_combinator_only_parameters(params: Dict[str, Any]) -> bool:
    props = params.get("properties")
    if isinstance(props, dict) and props:
        return False
    return bool(params.get("anyOf") or params.get("oneOf") or params.get("allOf"))
