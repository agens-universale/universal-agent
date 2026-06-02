from __future__ import annotations

import json
import logging
from typing import Any

from intent_scope.agent_logging import (
    log_intent_scope_delta_appended,
    log_intent_scope_skipped,
    log_intent_scope_summary_done,
)
from config_modules.intent_scope import (
    intent_scope_active,
    intent_scope_tool_record,
    intent_scope_tool_store_flags,
)
from intent_scope.append import append_tool_aggregated_to_current, append_tool_raw_to_current
from intent_scope.raw_payload import truncate_raw_tool_payload
from intent_scope.summarizer import (
    build_fallback_aggregated_content,
    summarize_tool_intent_line,
)

_logger = logging.getLogger(__name__)


def _parse_tool_arguments(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {"_raw": text}


def _parse_tool_output(raw: str) -> Any | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _append_aggregated_tool_intent(
    *,
    tool_name: str,
    tool_output_raw: str,
    tool_arguments: str,
    state: Any,
    ctx: Any,
    log: Any,
) -> None:
    scope_cfg = getattr(ctx, "intent_scope", None)
    summary_line = summarize_tool_intent_line(
        ctx=ctx,
        state=state,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        tool_output_raw=tool_output_raw,
        logger=log,
    )
    used_fallback = False
    if not summary_line and scope_cfg is not None and scope_cfg.fallback_raw_on_llm_error:
        summary_line = build_fallback_aggregated_content(
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

    before = len(getattr(state, "current_user_intent", []) or [])
    appended = append_tool_aggregated_to_current(
        state, tool_name, summary_line, fallback=used_fallback
    )
    if appended:
        log_intent_scope_delta_appended(
            log, tool_name=tool_name, delta_chars=len(summary_line)
        )


def _append_raw_tool_intent(
    *,
    tool_name: str,
    tool_output_raw: str,
    tool_arguments: str,
    state: Any,
    ctx: Any,
    log: Any,
    store_arguments: bool,
    store_output: bool,
) -> None:
    scope_cfg = getattr(ctx, "intent_scope", None)
    max_raw = int(getattr(scope_cfg, "max_raw_record_chars", 0) or 0)
    arguments = _parse_tool_arguments(tool_arguments) if store_arguments else None
    output = _parse_tool_output(tool_output_raw) if store_output else None
    if store_arguments:
        arguments, output = truncate_raw_tool_payload(
            arguments or {}, output, max_chars=max_raw
        )
        if not store_output:
            output = None
    elif store_output:
        _, output = truncate_raw_tool_payload({}, output, max_chars=max_raw)
    appended = append_tool_raw_to_current(state, tool_name, arguments, output)
    if appended:
        payload: dict[str, Any] = {}
        if arguments is not None:
            payload["arguments"] = arguments
        if output is not None:
            payload["output"] = output
        log_intent_scope_delta_appended(
            log,
            tool_name=tool_name,
            delta_chars=len(json.dumps(payload, ensure_ascii=False)),
        )


def maybe_update_intent_scope_from_tool(
    *,
    tool_name: str,
    tool_output_raw: str,
    tool_arguments: str = "",
    state: Any,
    ctx: Any,
) -> None:
    """
    Post-tool hook: optional intent log append into ``current_user_intent`` (fail-open).
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

    record_mode = intent_scope_tool_record(scope_cfg, tool_name)
    if record_mode is None:
        log_intent_scope_skipped(log, tool_name=tool_name, reason="tool_not_listed")
        return
    store_arguments, store_output = intent_scope_tool_store_flags(scope_cfg, tool_name)
    if record_mode == "raw" and not store_arguments and not store_output:
        log_intent_scope_skipped(log, tool_name=tool_name, reason="raw_store_disabled")
        return
    if record_mode != "raw" and not str(tool_output_raw or "").strip():
        log_intent_scope_skipped(log, tool_name=tool_name, reason="empty_output")
        return
    if record_mode == "raw" and store_output and not str(tool_output_raw or "").strip():
        log_intent_scope_skipped(log, tool_name=tool_name, reason="empty_output")
        return

    if record_mode == "raw":
        _append_raw_tool_intent(
            tool_name=tool_name,
            tool_output_raw=tool_output_raw,
            tool_arguments=tool_arguments,
            state=state,
            ctx=ctx,
            log=log,
            store_arguments=store_arguments,
            store_output=store_output,
        )
        return

    _append_aggregated_tool_intent(
        tool_name=tool_name,
        tool_output_raw=tool_output_raw,
        tool_arguments=tool_arguments,
        state=state,
        ctx=ctx,
        log=log,
    )
