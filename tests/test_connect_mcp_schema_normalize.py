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
    assert normalized["anyOf"] == [{"type": "integer"}, {"type": "null"}]
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
    assert limit["anyOf"] == [{"type": "integer"}, {"type": "null"}]
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
