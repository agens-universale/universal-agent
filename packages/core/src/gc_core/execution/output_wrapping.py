from __future__ import annotations

import logging
from typing import Any, Optional

from gc_core.config_modules.stored_outputs import (
    get_tool_output_inline_max_chars,
)
from gc_core.constants.tool_names import (
    FINISH_TOOL_NAME,
    READ_STORED_TOOL_OUTPUT_NAME,
)
from gc_core.execution.output_envelope import (
    PAGINATION_EOF_MARKER_DOC,
    dumps_stored_tool_output_payload,
    utf8_byte_length,
)
from gc_core.execution.output_store import (
    ToolCallOutputRecord,
)
from gc_core.execution.state import ExecutionState as State
from gc_core.utils.generate_message_id import generate_msg_id_sha256

_fallback_logger = logging.getLogger(__name__)


def maybe_store_and_wrap_tool_output(
    tool_res: str,
    *,
    call_id: str,
    tool_name: str,
    state: State,
    settings: Any,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Длинный output — в stored-envelope. ``settings`` — всё, что принимает
    ``settings_of`` (контекст, настройки)."""
    log = logger if isinstance(logger, logging.Logger) else _fallback_logger
    inline_limit = get_tool_output_inline_max_chars(settings)
    if tool_name == "skills_load":
        inline_limit = max(inline_limit, 40000)
    if state.tool_call_outputs_store.is_disabled():
        return tool_res
    # Управляющие ответы нельзя подменять envelope с собственным ok=True:
    # вызывающий код принимает решение о границе хода по исходному ok.
    if tool_name in (READ_STORED_TOOL_OUTPUT_NAME, FINISH_TOOL_NAME):
        return tool_res

    total_chars = len(tool_res)
    if total_chars <= inline_limit:
        return tool_res

    output_ref = generate_msg_id_sha256("out")
    evicted = state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref=output_ref,
            source_tool=tool_name,
            call_id=call_id,
            total_chars=total_chars,
            text=tool_res,
            truncated_in_message=True,
        )
    )
    log.debug(
        "stored_tool_output_created output_ref=%s source_tool=%s call_id=%s total_chars=%s inline_limit=%s",
        output_ref,
        tool_name,
        call_id,
        total_chars,
        inline_limit,
    )
    if evicted is not None:
        log.debug(
            "stored_tool_output_evicted output_ref=%s source_tool=%s call_id=%s total_chars=%s",
            evicted.output_ref,
            evicted.source_tool,
            evicted.call_id,
            evicted.total_chars,
        )

    content = tool_res[:inline_limit]
    next_offset = len(content)
    return dumps_stored_tool_output_payload(
        ok=True,
        stored=True,
        output_ref=output_ref,
        from_original_call_id=call_id,
        source_tool=tool_name,
        offset=0,
        limit=inline_limit,
        total_chars=total_chars,
        total_bytes=utf8_byte_length(tool_res),
        truncated=next_offset < total_chars,
        next_offset=next_offset,
        content=content,
        hint=(
            "The original source_tool output is too large, so this response contains only part. "
            f"Continue with {READ_STORED_TOOL_OUTPUT_NAME} using output_ref and offset=next_offset "
            "while truncated=true. "
            f"{PAGINATION_EOF_MARKER_DOC} "
            "If the record is gone (LRU), re-invoke the original source_tool - the ref cannot be restored."
        ),
    )
