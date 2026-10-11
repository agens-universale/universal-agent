import json

from gc_core.constants.tool_names import READ_STORED_TOOL_OUTPUT_NAME
from gc_core.execution.output_envelope import (
    build_stored_tool_output_payload,
    dumps_stored_tool_output_payload,
    utf8_byte_length,
)


def test_truncated_page_includes_required_metadata_fields():
    text = "abcdefghij"
    content = text[:5]
    payload = json.loads(
        dumps_stored_tool_output_payload(
            ok=True,
            stored=True,
            output_ref="ref-1",
            offset=0,
            limit=5,
            total_chars=len(text),
            total_bytes=utf8_byte_length(text),
            truncated=True,
            next_offset=len(content),
            content=content,
            source_tool="some_tool",
            from_original_call_id="call-1",
        )
    )
    assert payload["truncated"] is True
    assert payload["total_chars"] == 10
    assert payload["total_bytes"] == len(text.encode("utf-8"))
    assert payload["output_ref"] == "ref-1"
    assert payload["offset"] == 0
    assert payload["returned_chars"] == 5
    assert payload["next_offset"] == 5
    assert payload["read_via"] == READ_STORED_TOOL_OUTPUT_NAME


def test_final_page_truncated_false_still_has_metadata():
    payload = build_stored_tool_output_payload(
        ok=True,
        stored=True,
        output_ref="ref-2",
        offset=8,
        limit=5,
        total_chars=10,
        total_bytes=10,
        truncated=False,
        next_offset=10,
        content="ij",
    )
    assert payload["truncated"] is False
    assert payload["returned_chars"] == 2
    assert payload["next_offset"] == payload["total_chars"]
