from __future__ import annotations

"""Stored tool output pagination envelopes.

Pagination contract (success pages):
- ``truncated=true``: more text remains; continue with ``offset=next_offset``.
- ``truncated=false`` (EOF): ``next_offset == total_chars`` — end of stored body;
  do not read further for the same ``output_ref``.
"""

import json
from typing import Any, Dict, Optional

from gc_core.constants.tool_names import READ_STORED_TOOL_OUTPUT_NAME

PAGINATION_EOF_MARKER_DOC = (
    "On the last page, truncated=false and next_offset equals total_chars (EOF marker); "
    "do not read further for the same output_ref."
)


def utf8_byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


def build_stored_tool_output_payload(  # noqa: PLR0913  # S107: won't fix, см. CONTRIBUTING.md
    *,
    ok: bool,
    stored: bool,
    output_ref: str,
    offset: int,
    limit: int,
    content: str,
    truncated: bool,
    next_offset: int,
    total_chars: Optional[int] = None,
    total_bytes: Optional[int] = None,
    source_tool: Optional[str] = None,
    from_original_call_id: Optional[str] = None,
    tool: Optional[str] = None,
    error: Optional[Dict[str, Any]] = None,
    hint: str = "",
    reason: str = "source_tool_output_too_large",
    read_via: str = READ_STORED_TOOL_OUTPUT_NAME,
) -> Dict[str, Any]:
    returned_chars = len(content)
    payload: Dict[str, Any] = {
        "type": "stored_tool_output",
        "ok": ok,
        "stored": stored,
        "reason": reason,
        "output_ref": output_ref,
        "offset": offset,
        "limit": limit,
        "returned_chars": returned_chars,
        "truncated": truncated,
        "next_offset": next_offset,
        "content": content,
        "read_via": read_via,
        "hint": hint,
        "error": error,
    }
    if tool is not None:
        payload["tool"] = tool
    if from_original_call_id is not None:
        payload["from_original_call_id"] = from_original_call_id
    if source_tool is not None:
        payload["source_tool"] = source_tool
    if total_chars is not None:
        payload["total_chars"] = total_chars
    if total_bytes is not None:
        payload["total_bytes"] = total_bytes
    return payload


def dumps_stored_tool_output_payload(**kwargs: Any) -> str:
    return json.dumps(
        build_stored_tool_output_payload(**kwargs), ensure_ascii=False
    )
