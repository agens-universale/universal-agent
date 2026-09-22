import logging
from typing import Any, Dict, Optional

from gc_core.config_modules.stored_outputs import get_tool_output_inline_max_chars
from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
from gc_core.tools.session.stored_outputs.envelope import (
    PAGINATION_EOF_MARKER_DOC,
    dumps_stored_tool_output_payload,
    utf8_byte_length,
)
from gc_core.tools.simple_tool_base import AbstractSimpleTool

_TOOL_NAME = "session_read_stored_tool_output"
_fallback_logger = logging.getLogger(__name__)

_NOT_FOUND_MESSAGE = (
    "Stored output was evicted or not found. Re-invoke the original source tool to "
    "regenerate output_ref; retrying session_read_stored_tool_output with the same ref will not work."
)


@loop_warn_on_repeat(**READ_PROFILE)
class ReadStoredToolOutput(AbstractSimpleTool):
    def _logger(self) -> logging.Logger:
        logger = getattr(self._ctx, "logger", None)
        if isinstance(logger, logging.Logger):
            return logger
        return _fallback_logger

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": _TOOL_NAME,
            "description": (
                "Read a paginated fragment of a previously stored large tool output by output_ref. "
                "Pagination uses **characters** (Unicode code points): `offset` + `limit`. "
                "Response pages include truncated, total_chars, next_offset, returned_chars. "
                "EOF: when truncated=false, next_offset equals total_chars (end of body)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output_ref": {
                        "type": "string",
                        "description": "Reference from stored_tool_output envelope.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": ("Character offset (Unicode code points) into the stored body. Default: 0."),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max characters to return from offset (not lines or records). "
                            "Clamped to tool_output_inline_max_chars."
                        ),
                    },
                },
                "required": ["output_ref"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> str:
        return """Tool name: session_read_stored_tool_output
Tool description: Read paginated chunks from stored large tool output using output_ref.

Input schema:
{
  "type": "object",
  "properties": {
    "output_ref": {"type": "string"},
    "offset": {"type": "integer"},
    "limit": {"type": "integer"}
  },
  "required": ["output_ref"],
  "additionalProperties": false
}

Behavior:
- offset default is 0.
- limit default is tool_output_inline_max_chars.
- limit is clamped to range [1, tool_output_inline_max_chars].
- Each page includes pagination metadata: truncated, total_chars, total_bytes, output_ref,
  offset, returned_chars, next_offset.
- More pages: truncated=true — continue with offset=next_offset.
- Last page (EOF): truncated=false and next_offset == total_chars — do not read further.
- If output_ref is missing/evicted, call the original source tool again to regenerate it.
"""

    def _inline_limit(self) -> int:
        return get_tool_output_inline_max_chars(self._config_dict or {})

    @classmethod
    def _payload(cls, **fields: Any) -> str:
        """Конверт stored-output с полями этого тула; набор полей — см. envelope."""
        return dumps_stored_tool_output_payload(
            **fields,
            tool=cls.registered_tool_name(),
            read_via=cls.registered_tool_name(),
        )

    async def acall(
        self,
        output_ref: str,
        offset: int = 0,
        limit: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        log = self._logger()
        state = self._state
        inline_limit = self._inline_limit()
        req_limit = inline_limit if limit is None else limit

        if not isinstance(output_ref, str) or not output_ref.strip():
            return self._payload(
                ok=False,
                stored=False,
                output_ref=str(output_ref),
                source_tool=None,
                from_original_call_id=None,
                offset=0,
                limit=inline_limit,
                total_chars=None,
                truncated=False,
                next_offset=0,
                content="",
                error={
                    "type": "invalid_argument",
                    "field": "output_ref",
                    "message": "output_ref must be a non-empty string.",
                },
                hint="Invalid output_ref argument.",
            )

        if not isinstance(offset, int):
            return self._payload(
                ok=False,
                stored=False,
                output_ref=output_ref,
                source_tool=None,
                from_original_call_id=None,
                offset=0,
                limit=inline_limit,
                total_chars=None,
                truncated=False,
                next_offset=0,
                content="",
                error={"type": "invalid_argument", "field": "offset", "message": "offset must be an integer >= 0."},
                hint="Invalid offset argument.",
            )

        if not isinstance(req_limit, int):
            return self._payload(
                ok=False,
                stored=False,
                output_ref=output_ref,
                source_tool=None,
                from_original_call_id=None,
                offset=max(0, offset),
                limit=inline_limit,
                total_chars=None,
                truncated=False,
                next_offset=0,
                content="",
                error={"type": "invalid_argument", "field": "limit", "message": "limit must be an integer >= 1."},
                hint="Invalid limit argument.",
            )

        if offset < 0:
            return self._payload(
                ok=False,
                stored=False,
                output_ref=output_ref,
                source_tool=None,
                from_original_call_id=None,
                offset=offset,
                limit=inline_limit,
                total_chars=None,
                truncated=False,
                next_offset=0,
                content="",
                error={"type": "invalid_argument", "field": "offset", "message": "offset must be >= 0."},
                hint="Invalid offset argument.",
            )

        if req_limit < 1:
            return self._payload(
                ok=False,
                stored=False,
                output_ref=output_ref,
                source_tool=None,
                from_original_call_id=None,
                offset=offset,
                limit=req_limit,
                total_chars=None,
                truncated=False,
                next_offset=0,
                content="",
                error={"type": "invalid_argument", "field": "limit", "message": "limit must be >= 1."},
                hint="Invalid limit argument.",
            )

        effective_limit = min(req_limit, inline_limit)
        store = getattr(state, "tool_call_outputs_store", None)
        if store is None:
            return self._payload(
                ok=False,
                stored=False,
                output_ref=output_ref,
                source_tool=None,
                from_original_call_id=None,
                offset=offset,
                limit=effective_limit,
                total_chars=None,
                truncated=False,
                next_offset=0,
                content="",
                error={
                    "type": "not_found",
                    "message": _NOT_FOUND_MESSAGE,
                    "recover_by": "reinvoke_source_tool",
                },
                hint="Stored record is unavailable. Re-invoke source_tool to regenerate output_ref.",
            )

        record = store.get(output_ref)
        if record is None:
            log.debug(
                "stored_tool_output_not_found output_ref=%s offset=%s limit=%s",
                output_ref,
                offset,
                effective_limit,
            )
            return self._payload(
                ok=False,
                stored=False,
                output_ref=output_ref,
                source_tool=None,
                from_original_call_id=None,
                offset=offset,
                limit=effective_limit,
                total_chars=None,
                truncated=False,
                next_offset=0,
                content="",
                error={
                    "type": "not_found",
                    "message": _NOT_FOUND_MESSAGE,
                    "recover_by": "reinvoke_source_tool",
                },
                hint="Stored record is unavailable. Re-invoke source_tool to regenerate output_ref.",
            )

        total_chars = record.total_chars
        safe_offset = min(offset, total_chars)
        content = record.text[safe_offset : safe_offset + effective_limit]
        next_offset = safe_offset + len(content)
        truncated = next_offset < total_chars
        log.debug(
            "stored_tool_output_read_ok output_ref=%s source_tool=%s call_id=%s offset=%s limit=%s returned_chars=%s next_offset=%s truncated=%s",
            output_ref,
            record.source_tool,
            record.call_id,
            safe_offset,
            effective_limit,
            len(content),
            next_offset,
            truncated,
        )
        if truncated:
            hint = (
                "Paged fragment of a large stored tool output. "
                "Continue with output_ref and offset=next_offset while truncated=true. "
                "If record is gone (LRU), re-invoke source_tool."
            )
        else:
            hint = f"Last page of stored tool output (EOF). {PAGINATION_EOF_MARKER_DOC}"
        return self._payload(
            ok=True,
            stored=True,
            output_ref=output_ref,
            source_tool=record.source_tool,
            from_original_call_id=record.call_id,
            offset=safe_offset,
            limit=effective_limit,
            total_chars=total_chars,
            total_bytes=utf8_byte_length(record.text),
            truncated=truncated,
            next_offset=next_offset,
            content=content,
            error=None,
            hint=hint,
        )


__all__ = ["ReadStoredToolOutput"]
