"""Internal schema markers (x-*) must not reach the provider.

``x-optional-keys`` / ``x-openai-strict`` / ``x-preserve-empty-string`` are our own
bookkeeping: the local validator reads them to decide which keys may be omitted. The
JSON Schema that goes on the wire must stay standard — and tools_data must not be
mutated by the transport, or the validator would lose the markers it depends on.
"""
import copy
import json

import pytest

from gc_core.tools import abuild_tools_data
from gc_core.tools.schema_optional import resolve_optional_property_keys
from gc_core.utils.connect_backend import normalize_chat_completions_tools

CONFIG = {
    "inject_tool_fields": {"reason": "One short sentence explaining this call."},
    "stop_tools": [],
    "openai_strict_tools_schema": False,
}


@pytest.fixture(scope="module")
async def specs() -> list:
    """The registry exactly as it ships — the payload we care about is the real one.

    Module-scoped: abuild_tools_data walks the skills registry and MCP discovery, so
    build it once for the whole file rather than per test.
    """
    _, built, *_ = await abuild_tools_data(CONFIG)
    return built


def _has_internal_marker(node) -> bool:
    if isinstance(node, dict):
        if any(isinstance(k, str) and k.startswith("x-") for k in node):
            return True
        return any(_has_internal_marker(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_internal_marker(v) for v in node)
    return False


def test_wire_payload_carries_no_internal_markers(specs):
    assert _has_internal_marker(specs), "sanity: specs are expected to carry markers before normalization"

    wire = normalize_chat_completions_tools(specs)

    assert wire, "normalization must not drop the tools"
    assert not _has_internal_marker(wire), (
        "internal x-* markers leaked into the provider payload: "
        f"{[t['function']['name'] for t in wire if _has_internal_marker(t)][:5]}"
    )


def test_nested_markers_are_stripped_too(specs):
    """fs_apply_file_edits keeps its markers inside edits[].items."""
    wire = normalize_chat_completions_tools(specs)

    edits = next(t for t in wire if t["function"]["name"] == "fs_apply_file_edits")
    item = edits["function"]["parameters"]["properties"]["edits"]["items"]
    assert "x-optional-keys" not in item
    assert item["required"] == [
        "search",
        "replace",
    ], "the standard part of the schema must survive untouched"


def test_normalization_does_not_mutate_tools_data(specs):
    before = copy.deepcopy(specs)

    normalize_chat_completions_tools(specs)

    assert specs == before, "transport must not strip markers from the source specs"
    read_file = next(s for s in specs if s["name"] == "fs_read_file")
    assert "reason" in resolve_optional_property_keys(read_file["parameters"]), (
        "local validation still needs the markers after a request was built"
    )


def test_wire_payload_is_json_serializable_and_standard(specs):
    wire = normalize_chat_completions_tools(specs)

    payload = json.loads(json.dumps({"tools": wire}))
    fn = next(t for t in payload["tools"] if t["function"]["name"] == "fs_read_file")["function"]
    assert set(fn["parameters"]) <= {"type", "properties", "required", "additionalProperties"}
