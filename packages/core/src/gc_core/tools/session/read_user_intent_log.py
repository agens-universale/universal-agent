import json
from typing import Any, Dict, Optional

from gc_core.config_modules.intent_scope import DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT, intent_scope_from_config_dict
from gc_core.intent_log import deserialize_list, read_info, read_page
from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
from gc_core.tools.simple_tool_base import AbstractSimpleTool

_TOOL_NAME = "session_read_user_intent_log"


@loop_warn_on_repeat(**READ_PROFILE)
class ReadUserIntentLog(AbstractSimpleTool):
    tool_name = _TOOL_NAME

    def _default_limit(self) -> int:
        cfg = intent_scope_from_config_dict(self._config_dict or {})
        return int(
            cfg.read_page_record_limit or DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT
        )

    def _closed_log(self) -> list:
        if self._state is None:
            return []
        raw = getattr(self._state, "user_intent_log", None) or []
        return deserialize_list(raw)

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        default_records = DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT
        return {
            "type": "function",
            "name": _TOOL_NAME,
            "description": (
                "Read-only access to the closed user intent log (completed turns). "
                "Pagination for action=read uses **records** (JSONL entries), not lines or characters. "
                "action=info returns totals; action=read returns a page from record index offset. "
                "Open-turn data in current_user_intent is not included."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["info", "read"],
                        "description": (
                            "info = total_chars and total_records; "
                            "read = paginated JSONL page (offset/limit in records)."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "For action=read only. 0-based **record** index (not char/line). Default: 0."
                        ),
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"For action=read only. Max records per page (default {default_records})."
                        ),
                        "default": default_records,
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> str:
        page_limit = DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT
        return f"""Tool name: session_read_user_intent_log
Tool description: Read-only paginated access to closed user_intent_log (completed turns).

Input schema:
{{"type":"object","properties":{{"action":{{"type":"string","enum":["info","read"]}},"offset":{{"type":"integer","default":0}},"limit":{{"type":"integer","default":{page_limit}}}}},"required":["action"],"additionalProperties":false}}

Behavior:
- Source: state.user_intent_log only (not current_user_intent).
- action=info: total_chars, total_records, default_limit (suggested records per read page).
- action=read: offset/limit in **records** (default limit {page_limit}), content (valid JSONL), next_offset, truncated, page_chars, page_records.
- Same record offset/limit contract as dialog_summary intent log tail preview.
"""

    def _payload(self, *, ok: bool, action: str, body: Dict[str, Any]) -> str:
        payload: Dict[str, Any] = {
            "tool": ReadUserIntentLog.registered_tool_name(),
            "ok": ok,
            "action": action,
        }
        payload.update(body)
        return json.dumps(payload, ensure_ascii=False)

    def _error(self, *, action: str, error_type: str, message: str) -> str:
        return self._payload(
            ok=False,
            action=action,
            body={"error": {"type": error_type, "message": message}},
        )


    async def acall(
        self,
        action: str,
        offset: int = 0,
        limit: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        if self._state is None:
            return self._error(
                action=action,
                error_type="no_state",
                message="state is missing.",
            )
        if kwargs:
            return self._error(
                action=action,
                error_type="unexpected_arguments",
                message=f"Unexpected arguments: {', '.join(sorted(kwargs.keys()))}.",
            )

        default_limit = self._default_limit()
        records = self._closed_log()

        if action == "info":
            info = read_info(records, default_limit=default_limit)
            return self._payload(ok=True, action="info", body={**info, "error": None})

        if action != "read":
            return self._error(
                action=action,
                error_type="invalid_action",
                message=f"Unknown action: {action}",
            )

        if not isinstance(offset, int):
            return self._error(
                action="read",
                error_type="invalid_argument",
                message="offset must be an integer >= 0.",
            )
        if offset < 0:
            return self._error(
                action="read",
                error_type="invalid_argument",
                message="offset must be >= 0.",
            )

        req_limit = default_limit if limit is None else limit
        if not isinstance(req_limit, int):
            return self._error(
                action="read",
                error_type="invalid_argument",
                message="limit must be an integer >= 0.",
            )
        if req_limit < 0:
            return self._error(
                action="read",
                error_type="invalid_argument",
                message="limit must be >= 0.",
            )

        page = read_page(
            records,
            offset,
            req_limit,
            default_limit=default_limit,
        )
        return self._payload(ok=True, action="read", body={**page, "error": None})


__all__ = ["ReadUserIntentLog"]
