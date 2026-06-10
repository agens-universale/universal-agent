"""
Подготовка MCP-инструментов в формате tools_data для агента:
{ tool_name: {"tool_fabric": ..., "spec": ..., "description": ...} }
"""
import copy
import logging
from typing import Any, Dict

from tools.connect_mcp.api import call_mcp_tool
from tools.connect_mcp.registry import ToolsCache

logger = logging.getLogger(__name__)


class MCPToolWrapper:
    """
    Обёртка над MCP-инструментом с интерфейсом как у локальных тулов:
    __call__(**arguments) -> str | dict | list.
    """

    def __init__(
        self,
        full_tool_name: str,
        servers_cfg: Dict[str, Dict[str, Any]],
        tools_registry: Dict[str, Dict[str, Any]],
        timeout: float,
        default_use_retry: bool,
    ) -> None:
        self._full_tool_name = full_tool_name
        self._servers_cfg = servers_cfg
        self._tools_registry = tools_registry
        self._timeout = timeout
        self._default_use_retry = default_use_retry

    def __call__(self, **arguments: Any) -> Any:
        return call_mcp_tool(
            full_tool_name=self._full_tool_name,
            arguments=arguments,
            servers_cfg=self._servers_cfg,
            tools_registry=self._tools_registry,
            timeout=self._timeout,
            default_use_retry=self._default_use_retry,
        )


def _resolve_schema_refs(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    """Inline ``#/$defs/...`` refs (OpenAI strict rejects bare ``$ref`` property schemas)."""
    if depth > 64:
        return node
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.rsplit("/", 1)[-1]
            target = defs.get(name)
            if isinstance(target, dict):
                resolved = _resolve_schema_refs(target, defs, depth + 1)
                merged = dict(resolved) if isinstance(resolved, dict) else {}
                for key, value in node.items():
                    if key != "$ref" and key not in merged:
                        merged[key] = value
                return merged
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$defs":
                continue
            out[key] = _resolve_schema_refs(value, defs, depth + 1)
        return out
    if isinstance(node, list):
        return [_resolve_schema_refs(item, defs, depth + 1) for item in node]
    return node


def _inline_parameter_schema_refs(parameters: Dict[str, Any]) -> None:
    """Expand Pydantic/FastMCP ``$defs`` before property normalization."""
    defs = parameters.get("$defs")
    if not isinstance(defs, dict) or not defs:
        return
    resolved = _resolve_schema_refs(parameters, defs)
    if isinstance(resolved, dict):
        parameters.clear()
        parameters.update(resolved)


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

    # Schema-combinator constructs (anyOf / oneOf / allOf) define the type via
    # their branches; a top-level `type` is optional in JSON Schema and adding
    # one here breaks validation when it conflicts with the combinator.
    # Example: {"anyOf": [{"type": "integer"}, {"type": "null"}]} for `int | None`
    # would otherwise gain `type: "string"` here, making any value invalid
    # (validator AND-s `type` with `anyOf`).
    has_combinator = any(k in schema for k in ("anyOf", "oneOf", "allOf"))
    has_ref = isinstance(schema.get("$ref"), str)

    if "type" not in schema and not has_combinator and not has_ref:
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
    else:
        # Note: actual MCP tool execution output is normalized by tools.connect_mcp.api.call_mcp_tool
        # into a stable JSON envelope. Keep this short to avoid bloating tool descriptions.
        spec["description"] = spec["description"].strip()

    # Ensure the agent knows about the normalized return format for ALL MCP tools.
    # (MCP servers may return arbitrary shapes; we wrap them consistently.)
    spec["description"] += (
        " Return format: JSON string envelope with fields {tool, ok, mcp_server, mcp_tool, arguments, result, error}. "
        "Binary payloads are replaced with the string 'binary data'."
    )

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

    _inline_parameter_schema_refs(parameters)

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

    _inline_parameter_schema_refs(parameters)

    spec["parameters"] = parameters
    spec.setdefault("strict", True)

    return spec



def get_mcp_tools_data_for_config(
    config_dict: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """
    Собрать словарь MCP-инструментов в формате tools_data для заданного config_dict.
    """
    servers_cfg = config_dict.get("mcp-servers") or {}
    if not servers_cfg:
        return {}

    cfg: Dict[str, Any] = {
        "mcp-servers": servers_cfg,
        "mcp-meta": config_dict.get("mcp-meta") or {},
        "use_retry": bool(config_dict.get("use_retry", False)),
    }

    tools_cache = ToolsCache(cfg)

    try:
        mcp_registry = tools_cache.get_tools()
    except Exception:
        logger.error("Failed to load MCP tools", exc_info=True)
        return {}

    timeout = (
        config_dict.get("mcp-meta", {}).get("request_timeout_sec")
        or 600
    )

    default_use_retry = bool(config_dict.get("use_retry", False))

    def make_fabric(full_tool_name: str):
        def fabric(**_kwargs: Any) -> MCPToolWrapper:
            return MCPToolWrapper(
                full_tool_name,
                servers_cfg,
                mcp_registry,
                timeout,
                default_use_retry=default_use_retry,
            )

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
    "get_mcp_tools_data_for_config",
]
