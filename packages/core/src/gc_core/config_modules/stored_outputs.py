from __future__ import annotations

from typing import Any, Tuple

DEFAULT_TOOL_OUTPUT_INLINE_MAX_CHARS = 10000
DEFAULT_STORED_TOOL_OUTPUTS_MAX_ENTRIES = 50


def normalize_tool_output_limits(
    inline_max_chars: Any, stored_max_entries: Any
) -> Tuple[int, int]:
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


def _agent_limits(config: Any) -> Tuple[int, int]:
    """(inline, entries) из ``agent`` настроек — всё, что принимает ``settings_of``."""
    # Ленивый импорт: settings → root → этот модуль.
    from gc_core.settings import settings_of

    agent = settings_of(config).agent
    return normalize_tool_output_limits(
        agent.tool_output_inline_max_chars,
        agent.stored_tool_outputs_max_entries,
    )


def get_tool_output_inline_max_chars(config: Any) -> int:
    inline, _ = _agent_limits(config)
    return inline


def get_stored_tool_outputs_max_entries(config: Any) -> int:
    _, entries = _agent_limits(config)
    return entries
