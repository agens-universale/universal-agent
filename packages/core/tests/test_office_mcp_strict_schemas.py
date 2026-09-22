"""All office-mcp tool schemas must survive OpenAI strict normalization."""

from __future__ import annotations

import pytest

# office_mcp — внешний MCP (external/office-mcp), ставится через
# recommended_mcp/install.py. Если не установлен — скипаем модуль, а не
# роняем сбор всего сьюта.
pytest.importorskip("office_mcp")

from gc_core.tools.connect_mcp.agent_tools import normalize_tool_spec_for_agent
from mcp.server.fastmcp.utilities.func_metadata import func_metadata
from office_mcp.server import ServerConfig, build_server


def _ref_with_sibling_errors(node: object, path: tuple[str, ...] = ()) -> list[str]:
    errors: list[str] = []
    if isinstance(node, dict):
        if "$ref" in node and len(node) > 1:
            errors.append(f"{path}: $ref has sibling keys {sorted(k for k in node if k != '$ref')}")
        for key, value in node.items():
            if key != "$defs":
                errors.extend(_ref_with_sibling_errors(value, path + (key,)))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            errors.extend(_ref_with_sibling_errors(value, path + (f"[{idx}]",)))
    return errors


def _dangling_ref_errors(
    node: object,
    defs: object,
    path: tuple[str, ...] = (),
) -> list[str]:
    errors: list[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.rsplit("/", 1)[-1]
            if not isinstance(defs, dict) or name not in defs:
                errors.append(f"{path}: dangling $ref {ref!r}")
        for key, value in node.items():
            if key != "$defs":
                errors.extend(_dangling_ref_errors(value, defs, path + (key,)))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            errors.extend(_dangling_ref_errors(value, defs, path + (f"[{idx}]",)))
    return errors


def _strict_schema_errors(node: object, path: tuple[str, ...] = ()) -> list[str]:
    errors: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                errors.append(f"{path}: additionalProperties must be false")
            props = node.get("properties") or {}
            req = node.get("required") or []
            missing = set(props) - set(req)
            if missing:
                errors.append(f"{path}: required missing {sorted(missing)}")
        items = node.get("items")
        if isinstance(items, dict) and not any(k in items for k in ("type", "anyOf", "oneOf", "allOf", "$ref")):
            errors.append(f"{path}.items: missing type")
        for key, value in node.items():
            if key != "$defs":
                errors.extend(_strict_schema_errors(value, path + (key,)))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            errors.extend(_strict_schema_errors(value, path + (f"[{idx}]",)))
    return errors


def test_all_office_mcp_tools_normalize_for_openai_strict() -> None:
    cfg = ServerConfig(
        allowed_dirs=["/tmp"],
        allow_any=True,
        max_file_size=50 * 1024 * 1024,
        max_output_chars=1_000_000,
        timeout_s=60,
    )
    mcp = build_server(cfg)
    errors: list[str] = []
    for name, tool in mcp._tool_manager._tools.items():
        params = func_metadata(tool.fn).arg_model.model_json_schema()
        normalized = normalize_tool_spec_for_agent(
            {
                "type": "function",
                "name": f"mcp_office_{name}",
                "description": tool.description or "office tool",
                "parameters": params,
            },
            openai_strict=True,
        )
        params = normalized["parameters"]
        errors.extend(_strict_schema_errors(params, (name,)))
        errors.extend(_dangling_ref_errors(params, params.get("$defs"), (name, "parameters")))
        errors.extend(_ref_with_sibling_errors(params, (name, "parameters")))
    assert not errors, ";\n".join(errors)
