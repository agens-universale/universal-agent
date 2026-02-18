"""
Подготовка MCP-инструментов в формате tools_data для агента:
{ tool_name: {"tool_fabric": ..., "spec": ..., "description": ...} }
"""
import copy
import logging
from typing import Any, Dict

import config
from tools.connect_mcp.api import call_mcp_tool
from tools.connect_mcp.registry import get_all_tools

logger = logging.getLogger(__name__)


class MCPToolWrapper:
    """
    Обёртка над MCP-инструментом с интерфейсом как у локальных тулов:
    __call__(**arguments) -> str | dict | list.
    """

    def __init__(
        self,
        full_tool_name: str,
        tools_registry: Dict[str, Dict[str, Any]],
        timeout: float,
    ) -> None:
        self._full_tool_name = full_tool_name
        self._tools_registry = tools_registry
        self._timeout = timeout

    def __call__(self, **arguments: Any) -> Any:
        return call_mcp_tool(
            full_tool_name=self._full_tool_name,
            arguments=arguments,
            tools_registry=self._tools_registry,
            timeout=self._timeout,
        )


def _ensure_param_description(schema: Dict[str, Any], param_name: str) -> None:
    """
    Гарантировать наличие description у параметра.
    При отсутствии используем title или генерируем простое описание.
    """
    if "description" in schema and isinstance(schema["description"], str) and schema["description"].strip():
        return

    title = schema.get("title")
    if isinstance(title, str) and title.strip():
        schema["description"] = title.strip()
    else:
        schema["description"] = f"{param_name} parameter."


def _normalize_schema_recursive(schema: Any, param_name: str = "") -> Any:
    """
    Рекурсивная нормализация фрагмента JSON Schema.
    Поддерживает объекты, массивы и простые типы.
    """
    if not isinstance(schema, dict):
        return {
            "type": "string",
            "description": f"{param_name} parameter. Raw schema value: {schema!r}",
        }

    if "type" not in schema:
        if "properties" in schema:
            schema["type"] = "object"
        elif "items" in schema:
            schema["type"] = "array"
        else:
            schema["type"] = "string"

    schema_type = schema.get("type")

    if schema_type == "object":
        props = schema.get("properties")
        if not isinstance(props, dict):
            props = {}
            schema["properties"] = props

        required = schema.get("required")
        if not isinstance(required, list):
            required = []
            schema["required"] = required

        for name, subschema in list(props.items()):
            normalized_subschema = _normalize_schema_recursive(subschema, param_name=name)
            if not isinstance(normalized_subschema, dict):
                normalized_subschema = {
                    "type": "string",
                    "description": f"{name} parameter.",
                }
            _ensure_param_description(normalized_subschema, name)
            props[name] = normalized_subschema

        if "additionalProperties" not in schema:
            schema["additionalProperties"] = False

    elif schema_type == "array":
        items = schema.get("items")
        if items is not None:
            schema["items"] = _normalize_schema_recursive(items, param_name=f"{param_name}_item")

    else:
        _ensure_param_description(schema, param_name or "value")

    return schema


def normalize_tool_spec_for_agent(tool_spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Привести произвольную сигнатуру MCP-инструмента к стандарту агента.
    Гарантируются:
    - type: "function"
    - непустой description
    - parameters.type == "object"
    - для всех parameters.properties.* есть description и адекватный type
    """
    spec = copy.deepcopy(tool_spec) if isinstance(tool_spec, dict) else {}

    if spec.get("type") != "function":
        spec["type"] = "function"

    if not isinstance(spec.get("description"), str) or not spec["description"].strip():
        spec["description"] = "No description provided by MCP server."

    parameters = spec.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }

    if "type" not in parameters:
        parameters["type"] = "object"

    if parameters.get("type") != "object":
        parameters = {
            "type": "object",
            "properties": {
                "value": _normalize_schema_recursive(parameters, param_name="value"),
            },
            "required": ["value"],
            "additionalProperties": False,
        }

    props = parameters.get("properties")
    if not isinstance(props, dict):
        props = {}
        parameters["properties"] = props

    required = parameters.get("required")
    if not isinstance(required, list):
        required = []
        parameters["required"] = required

    for name, param_schema in list(props.items()):
        normalized = _normalize_schema_recursive(param_schema, param_name=name)
        if not isinstance(normalized, dict):
            normalized = {
                "type": "string",
                "description": f"{name} parameter.",
            }
        _ensure_param_description(normalized, name)
        props[name] = normalized

    if "additionalProperties" not in parameters:
        parameters["additionalProperties"] = False

    spec["parameters"] = parameters
    spec.setdefault("strict", True)

    return spec


def get_mcp_tools_data() -> Dict[str, Dict[str, Any]]:
    """
    Собрать словарь MCP-инструментов в формате tools_data.
    Ключ — полное имя тула (например rag_submit_solution),
    значение — {"tool_fabric": ..., "spec": ..., "description": ...}.
    Если MCP не настроен или загрузка не удалась, возвращается пустой dict.
    """
    servers_cfg = config.config_dict.get("mcp-servers") or {}
    if not servers_cfg:
        return {}

    try:
        mcp_registry = get_all_tools(servers_cfg)
    except Exception:
        logger.error("Failed to load MCP tools", exc_info=True)
        return {}

    timeout = (
        config.config_dict.get("mcp-meta", {}).get("request_timeout_sec")
        or 600
    )

    def make_fabric(full_tool_name: str):
        def fabric(**_kwargs: Any) -> MCPToolWrapper:
            return MCPToolWrapper(full_tool_name, mcp_registry, timeout)

        return fabric

    result: Dict[str, Dict[str, Any]] = {}
    for full_name, entry in mcp_registry.items():
        tool_entry = (
            entry.get("tool")
            if isinstance(entry, dict) and "tool" in entry
            else entry
        )
        if not isinstance(tool_entry, dict):
            continue

        normalized_spec = normalize_tool_spec_for_agent(tool_entry)

        result[full_name] = {
            "tool_fabric": make_fabric(full_name),
            "spec": normalized_spec,
            "description": normalized_spec.get("description", ""),
        }
    return result


__all__ = [
    "MCPToolWrapper",
    "get_mcp_tools_data",
]
