"""Truncate ``tool_result_raw`` payload before intent log append."""

from __future__ import annotations

import json
from typing import Any

from message_format.dialog_history import truncate_text

_RAW_OUTPUT_WRAPPER_OVERHEAD = 32


def _output_as_text(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False)


def truncate_raw_tool_payload(
    arguments: dict[str, Any],
    output: Any,
    *,
    max_chars: int,
) -> tuple[dict[str, Any], Any]:
    """Cap serialized ``{arguments, output?}`` for ``tool_result_raw`` persist."""
    if max_chars <= 0:
        return arguments, output

    args = dict(arguments or {})
    args_json = json.dumps(args, ensure_ascii=False)
    if len(args_json) >= max_chars:
        return {"_truncated": True}, truncate_text(args_json, max_chars)

    if output is None:
        return args, None

    out_text = _output_as_text(output)
    out_budget = max(max_chars - len(args_json) - _RAW_OUTPUT_WRAPPER_OVERHEAD, 1)
    if len(out_text) <= out_budget:
        return args, output

    return args, truncate_text(out_text, out_budget)
