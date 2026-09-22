from __future__ import annotations

from typing import Any, Dict, Tuple

DEFAULT_TOOL_OUTPUT_INLINE_MAX_CHARS = 10000
DEFAULT_STORED_TOOL_OUTPUTS_MAX_ENTRIES = 50


def normalize_tool_output_limits(inline_max_chars: Any, stored_max_entries: Any) -> Tuple[int, int]:
    try:
        inline = int(inline_max_chars)
    except (TypeError, ValueError):
        inline = DEFAULT_TOOL_OUTPUT_INLINE_MAX_CHARS
    if inline <= 0:
        inline = DEFAULT_TOOL_OUTPUT_INLINE_MAX_CHARS

    try:
        entries = int(stored_max_entries)
    except (TypeError, ValueError):
        entries = DEFAULT_STORED_TOOL_OUTPUTS_MAX_ENTRIES
    if entries < 0:
        entries = 1

    return inline, entries


def get_tool_output_inline_max_chars(config: Dict[str, Any]) -> int:
    inline, _ = normalize_tool_output_limits(
        config.get("tool_output_inline_max_chars"),
        config.get(
            "stored_tool_outputs_max_entries",
            DEFAULT_STORED_TOOL_OUTPUTS_MAX_ENTRIES,
        ),
    )
    return inline


def get_stored_tool_outputs_max_entries(config: Dict[str, Any]) -> int:
    _, entries = normalize_tool_output_limits(
        config.get("tool_output_inline_max_chars", DEFAULT_TOOL_OUTPUT_INLINE_MAX_CHARS),
        config.get("stored_tool_outputs_max_entries"),
    )
    return entries
