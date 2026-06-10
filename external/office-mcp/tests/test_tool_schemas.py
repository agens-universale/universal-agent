"""JSON Schema for writer models (OpenAI strict / closed objects)."""

from __future__ import annotations

from office_mcp.writer_schema import SheetSpec, WorkbookSpec


def test_workbook_spec_json_schema_is_closed_object() -> None:
    schema = WorkbookSpec.model_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["sheets"]


def test_sheet_spec_json_schema_is_closed_object() -> None:
    schema = SheetSpec.model_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
