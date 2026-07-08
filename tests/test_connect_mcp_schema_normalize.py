"""Regression tests for tools.connect_mcp.agent_tools schema normalization.

Reproduces a bug where _normalize_schema_recursive forced `type: "string"` on
parameter schemas that legitimately use `anyOf` / `oneOf` / `allOf` (e.g.
`int | None` Python type hints in MCP tool signatures), making JSON Schema
validation impossible because the validator requires both `type` AND `anyOf`
to match.
"""
from __future__ import annotations

from tools.connect_mcp.agent_tools import (
    _normalize_schema_recursive,
    normalize_tool_spec_for_agent,
)


def test_normalize_preserves_anyof_without_forcing_type() -> None:
    """`anyOf` schema should not get a contradictory `type: string` slapped on."""
    raw = {
        "anyOf": [{"type": "integer"}, {"type": "null"}],
        "default": None,
        "title": "Limit",
    }
    normalized = _normalize_schema_recursive(raw, param_name="limit")

    assert "anyOf" in normalized
    assert normalized["anyOf"][0]["type"] == "integer"
    assert normalized["anyOf"][1]["type"] == "null"
    # The bug: previously normalized["type"] was forced to "string".
    # After fix: either no `type`, or `type` value compatible with anyOf — but NOT
    # an unrelated default like "string".
    if "type" in normalized:
        assert normalized["type"] != "string", (
            "type='string' contradicts anyOf=[integer, null] and breaks validation"
        )


def test_normalize_preserves_oneof_without_forcing_type() -> None:
    raw = {
        "oneOf": [{"type": "integer"}, {"type": "null"}],
        "title": "X",
    }
    normalized = _normalize_schema_recursive(raw, param_name="x")
    assert "oneOf" in normalized
    if "type" in normalized:
        assert normalized["type"] != "string"


def test_normalize_preserves_allof_without_forcing_type() -> None:
    raw = {
        "allOf": [{"type": "integer"}],
        "title": "Y",
    }
    normalized = _normalize_schema_recursive(raw, param_name="y")
    assert "allOf" in normalized
    if "type" in normalized:
        assert normalized["type"] != "string"


def test_normalize_still_defaults_string_when_no_type_and_no_combinator() -> None:
    """Plain dicts without type/properties/items/anyOf still fall back to string."""
    raw = {"description": "Some param", "title": "Z"}
    normalized = _normalize_schema_recursive(raw, param_name="z")
    assert normalized["type"] == "string"


def test_normalize_object_with_anyof_property() -> None:
    """Top-level object has properties; one property uses anyOf."""
    raw = {
        "type": "object",
        "properties": {
            "limit": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "default": None,
                "title": "Limit",
            },
        },
        "required": [],
    }
    normalized = _normalize_schema_recursive(raw)
    limit_schema = normalized["properties"]["limit"]
    assert "anyOf" in limit_schema
    if "type" in limit_schema:
        assert limit_schema["type"] != "string"


def test_full_tool_spec_with_anyof_param_stays_valid() -> None:
    """End-to-end: normalize_tool_spec_for_agent doesn't break anyOf params."""
    tool_spec = {
        "type": "function",
        "name": "query_xlsx",
        "description": "Filter rows.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "limit": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "title": "Limit",
                },
            },
            "required": ["path"],
        },
        "strict": True,
    }
    normalized = normalize_tool_spec_for_agent(tool_spec)
    limit = normalized["parameters"]["properties"]["limit"]
    assert "anyOf" in limit
    assert limit["anyOf"][0]["type"] == "integer"
    assert limit["anyOf"][1]["type"] == "null"
    if "type" in limit:
        assert limit["type"] != "string"


def test_existing_explicit_type_is_preserved() -> None:
    """Regression: a schema that already declares its own `type` should not be touched."""
    raw = {"type": "integer", "title": "N"}
    normalized = _normalize_schema_recursive(raw, param_name="n")
    assert normalized["type"] == "integer"


def test_object_without_type_still_inferred() -> None:
    """Regression: dict with `properties` but no `type` should still be inferred as object."""
    raw = {"properties": {"x": {"type": "string"}}}
    normalized = _normalize_schema_recursive(raw, param_name="o")
    assert normalized["type"] == "object"


def test_array_without_type_still_inferred() -> None:
    """Regression: dict with `items` but no `type` should still be inferred as array."""
    raw = {"items": {"type": "string"}}
    normalized = _normalize_schema_recursive(raw, param_name="a")
    assert normalized["type"] == "array"


def test_inline_defs_ref_for_openai_strict_workbook() -> None:
    """Bare ``$ref`` properties lack additionalProperties; inlining fixes OpenAI strict."""
    tool_spec = {
        "type": "function",
        "name": "mcp_office_create_xlsx_file",
        "description": "Generate xlsx.",
        "parameters": {
            "type": "object",
            "$defs": {
                "WorkbookSpec": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "sheets": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["sheets"],
                },
            },
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "workbook": {"$ref": "#/$defs/WorkbookSpec"},
                "overwrite": {"type": "boolean", "default": False, "title": "Overwrite"},
            },
            "required": ["path", "workbook"],
        },
    }
    normalized = normalize_tool_spec_for_agent(tool_spec)
    workbook = normalized["parameters"]["properties"]["workbook"]
    assert workbook["type"] == "object"
    assert workbook["additionalProperties"] is False
    assert "sheets" in workbook["properties"]
    assert "$ref" not in workbook
    assert "$defs" not in normalized["parameters"]


def test_nested_object_required_includes_all_properties_for_openai_strict() -> None:
    """Pydantic optional fields must appear in nested ``required`` under strict mode."""
    cell_style = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "range": {"type": "string", "title": "Range"},
            "align": {
                "anyOf": [
                    {"enum": ["left", "center", "right"], "type": "string"},
                    {"type": "null"},
                ],
                "default": None,
                "title": "Align",
            },
            "bold": {
                "anyOf": [{"type": "boolean"}, {"type": "null"}],
                "default": None,
                "title": "Bold",
            },
        },
        "required": ["range"],
    }
    tool_spec = {
        "type": "function",
        "name": "mcp_office_create_xlsx_file",
        "description": "Generate xlsx.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "workbook": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "sheets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "name": {"type": "string"},
                                    "cell_styles": {
                                        "type": "array",
                                        "items": cell_style,
                                    },
                                },
                                "required": ["name", "cell_styles"],
                            },
                        },
                    },
                    "required": ["sheets"],
                },
            },
            "required": ["path", "workbook"],
        },
    }
    normalized = normalize_tool_spec_for_agent(tool_spec, openai_strict=True)
    cell_items = (
        normalized["parameters"]["properties"]["workbook"]["properties"]["sheets"]["items"]
        ["properties"]["cell_styles"]["items"]
    )
    assert set(cell_items["required"]) == {"range", "align", "bold"}
    assert cell_items["x-optional-keys"] == ["align", "bold"]
    assert set(normalized["parameters"]["required"]) == {"path", "workbook"}


def test_anyof_array_branch_items_get_type_for_openai_strict() -> None:
    """``list | None`` from Pydantic yields ``items: {}`` inside anyOf; OpenAI needs ``type``."""
    tool_spec = {
        "type": "function",
        "name": "mcp_office_query_xlsx",
        "description": "Query xlsx.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "filter": {
                    "anyOf": [
                        {"items": {}, "type": "array"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "title": "Filter",
                },
            },
            "required": ["path"],
        },
    }
    normalized = normalize_tool_spec_for_agent(tool_spec)
    array_branch = normalized["parameters"]["properties"]["filter"]["anyOf"][0]
    assert array_branch["type"] == "array"
    assert "type" in array_branch["items"]


def test_recursive_defs_ref_keeps_defs_for_openai_strict() -> None:
    """Recursive Pydantic models must not leave dangling ``$ref`` without ``$defs``."""
    tool_spec = {
        "type": "function",
        "name": "mcp_office_query_xlsx",
        "description": "Query xlsx.",
        "parameters": {
            "type": "object",
            "$defs": {
                "FilterClause": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "col": {"type": "string", "title": "Col"},
                        "op": {"type": "string", "title": "Op"},
                    },
                    "required": ["col", "op"],
                },
                "OrFilterGroup": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "or": {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"$ref": "#/$defs/FilterClause"},
                                    {"$ref": "#/$defs/OrFilterGroup"},
                                ],
                            },
                            "title": "Or",
                        },
                    },
                    "required": ["or"],
                },
            },
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "filter": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"$ref": "#/$defs/FilterClause"},
                                    {"$ref": "#/$defs/OrFilterGroup"},
                                ],
                            },
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "title": "Filter",
                },
            },
            "required": ["path"],
        },
    }
    normalized = normalize_tool_spec_for_agent(tool_spec)
    params = normalized["parameters"]
    filter_schema = params["properties"]["filter"]
    array_branch = filter_schema["anyOf"][0]
    or_group_branch = array_branch["items"]["anyOf"][1]
    assert or_group_branch["type"] == "object"
    nested_or = or_group_branch["properties"]["or"]["items"]["anyOf"][1]
    assert nested_or == {"$ref": "#/$defs/OrFilterGroup"}
    assert "FilterClause" in params["$defs"]
    assert "OrFilterGroup" in params["$defs"]
    assert params["$defs"]["OrFilterGroup"]["additionalProperties"] is False


def test_anyof_array_object_items_get_additional_properties_false() -> None:
    """``list[dict] | None`` must not keep ``additionalProperties: true`` under strict."""
    tool_spec = {
        "type": "function",
        "name": "mcp_office_query_xlsx",
        "description": "Query xlsx.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "sort": {
                    "anyOf": [
                        {
                            "items": {"additionalProperties": True, "type": "object"},
                            "type": "array",
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "title": "Sort",
                },
            },
            "required": ["path"],
        },
    }
    normalized = normalize_tool_spec_for_agent(tool_spec)
    items = normalized["parameters"]["properties"]["sort"]["anyOf"][0]["items"]
    assert items["additionalProperties"] is False


def test_non_strict_preserves_server_required_default() -> None:
    """Default (openai_strict=False) keeps the MCP server's own `required`.

    Regression: gc-cli used to force EVERY MCP-tool property into `required`
    (OpenAI strict mode) unconditionally, ignoring backend.openai_strict_tools_schema.
    Non-strict models (e.g. deepseek) then omitted "optional" params like
    base/page/per_page -> WrongFunctionArgumentsException. With the flag off,
    optional params must stay optional.
    """
    tool_spec = {
        "type": "function",
        "name": "mcp_gitverse_list_pull_requests",
        "description": "List PRs.",
        "parameters": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "base": {"type": "string", "description": "Filter (optional)"},
                "page": {"type": "integer", "default": 1},
                "per_page": {"type": "integer", "default": 30},
            },
            "required": ["owner", "repo"],
        },
    }
    ns = normalize_tool_spec_for_agent(tool_spec)  # default: openai_strict=False
    assert ns["parameters"]["required"] == ["owner", "repo"]
    assert ns.get("strict") is False

    s = normalize_tool_spec_for_agent(tool_spec, openai_strict=True)
    assert set(s["parameters"]["required"]) == {"owner", "repo", "base", "page", "per_page"}
    assert s.get("strict") is True
