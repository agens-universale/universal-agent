from __future__ import annotations

import logging
from typing import Any

from intent_scope.agent_logging import (
    log_intent_scope_delta_appended,
    log_intent_scope_skipped,
    log_intent_scope_summary_done,
)
from config_modules.intent_scope import intent_scope_active
from intent_scope.summarizer import (
    build_fallback_raw_line,
    summarize_tool_intent_line,
)

_logger = logging.getLogger(__name__)


def maybe_update_intent_scope_from_tool(
    *,
    tool_name: str,
    tool_output_raw: str,
    tool_arguments: str = "",
    state: Any,
    ctx: Any,
) -> None:
    """
    Post-tool hook: optional LLM delta into ``current_user_intent`` (fail-open).
    Does not modify tool output.
    """
    log = getattr(ctx, "logger", None) or _logger
    scope_cfg = getattr(ctx, "intent_scope", None)
    if scope_cfg is None:
        log_intent_scope_skipped(log, tool_name=tool_name, reason="no_intent_scope_on_ctx")
        return

    config_dict = getattr(ctx, "config_dict", {}) or {}
    if str(config_dict.get("backend_api_mode", "responses")).strip().lower() != "chat_completions":
        log_intent_scope_skipped(
            log, tool_name=tool_name, reason="backend_not_chat_completions"
        )
        return
    if not intent_scope_active(scope_cfg):
        log_intent_scope_skipped(log, tool_name=tool_name, reason="tools_empty")
        return
    if tool_name not in scope_cfg.tools:
        log_intent_scope_skipped(log, tool_name=tool_name, reason="tool_not_listed")
        return
    if not str(tool_output_raw or "").strip():
        log_intent_scope_skipped(log, tool_name=tool_name, reason="empty_output")
        return

    summary_line = summarize_tool_intent_line(
        ctx=ctx,
        state=state,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        tool_output_raw=tool_output_raw,
        logger=log,
    )
    used_fallback = False
    if not summary_line and scope_cfg.fallback_raw_on_llm_error:
        summary_line = build_fallback_raw_line(
            ctx=ctx, tool_output_raw=tool_output_raw
        )
        used_fallback = bool(summary_line)
        if used_fallback:
            log_intent_scope_summary_done(
                log,
                tool_name=tool_name,
                summary_chars=len(summary_line),
                used_fallback_raw=True,
            )

    if not summary_line:
        log_intent_scope_skipped(
            log, tool_name=tool_name, reason="empty_summary"
        )
        return

    append_delta = getattr(state, "append_intent_scope_delta", None)
    if not callable(append_delta):
        log.warning(
            "intent_scope append_intent_scope_delta_missing tool=%s",
            tool_name,
        )
        return

    before = len(getattr(state, "current_user_intent", []) or [])
    append_delta(summary_line, source=tool_name)
    after = len(getattr(state, "current_user_intent", []) or [])
    if after > before:
        log_intent_scope_delta_appended(
            log, tool_name=tool_name, delta_chars=len(summary_line)
        )
