from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from uuid import uuid4

from config_modules.stored_outputs import (
    get_tool_output_inline_max_chars,
)
from constants.tool_names import (
    EXEC_COMMAND_NAME,
    EXEC_PYTHON_NAME,
    READ_STORED_TOOL_OUTPUT_NAME,
)
from states import State
from tools.session.stored_outputs.envelope import (
    PAGINATION_EOF_MARKER_DOC,
    dumps_stored_tool_output_payload,
    utf8_byte_length,
)
from tools.session.stored_outputs.tool_call_outputs_store import ToolCallOutputRecord
_fallback_logger = logging.getLogger(__name__)


def maybe_store_and_wrap_tool_output(
    tool_res: str,
    *,
    call_id: str,
    tool_name: str,
    state: State,
    config: Dict[str, Any],
    logger: Optional[logging.Logger] = None,
) -> str:
    log = logger if isinstance(logger, logging.Logger) else _fallback_logger
    inline_limit = get_tool_output_inline_max_chars(config)
    if tool_name == "skills_load":
        inline_limit = max(inline_limit, 40000)
    if state.tool_call_outputs_store.is_disabled():
        return tool_res
    if tool_name == READ_STORED_TOOL_OUTPUT_NAME:
        return tool_res
    if tool_name in (EXEC_COMMAND_NAME, EXEC_PYTHON_NAME):
        # Exec envelopes self-truncate (head+tail preview + own output refs);
        # a second generic wrap would cut the envelope JSON mid-string.
        return tool_res

    total_chars = len(tool_res)
    if total_chars <= inline_limit:
        return tool_res

    output_ref = str(uuid4())
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
