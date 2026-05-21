from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from config_modules.merge import deep_merge
from intent_scope.agent_logging import (
    log_intent_scope_summary_done,
    log_intent_scope_summary_start,
)
from config_modules.intent_scope import IntentScopeConfig
from message_format.dialog_history import truncate_text
from summarize.llm import request_summary_text
from utils.llm_chat_client import log_llm_prompt_and_result

_logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_BASE_DIR, "..", ".."))
_DEFAULT_PROMPT_PATH = os.path.join(
    _PROJECT_ROOT, "prompts", "intent_scope_tool_summary_prompt.md"
)

_INTENT_SCOPE_COMPLETION_OVERRIDES: Dict[str, Any] = {
    "separate_reasoning": False,
    "chat_template_kwargs": {"enable_thinking": False},
}


def build_intent_scope_payload_overrides(ctx: Any) -> Dict[str, Any]:
    summary_llm = getattr(ctx, "summary_llm", None)
    base: Dict[str, Any] = {}
    if summary_llm is not None:
        completion_kwargs = getattr(summary_llm, "completion_kwargs", None)
        if isinstance(completion_kwargs, dict):
            base = dict(completion_kwargs)
    return deep_merge(base, _INTENT_SCOPE_COMPLETION_OVERRIDES)


def _scope_cfg(ctx: Any) -> IntentScopeConfig:
    cfg = getattr(ctx, "intent_scope", None)
    if isinstance(cfg, IntentScopeConfig):
        return cfg
    return IntentScopeConfig()


def _resolve_prompt_path(ctx: Any) -> str:
    cfg = _scope_cfg(ctx)
    raw = str(cfg.intent_scope_tool_summary_prompt_path or "").strip()
    if not raw:
        return _DEFAULT_PROMPT_PATH
    if os.path.isabs(raw):
        return raw
    return os.path.abspath(os.path.join(_PROJECT_ROOT, raw))


def _load_system_prompt(ctx: Any) -> str:
    prompt_path = _resolve_prompt_path(ctx)
    if not os.path.exists(prompt_path):
        raise FileNotFoundError(
            f"Intent scope prompt file not found: {prompt_path}"
        )
    with open(prompt_path, "r", encoding="utf-8") as fd:
        system = fd.read().strip()
    if not system:
        raise ValueError(f"Intent scope prompt file is empty: {prompt_path}")
    return system


def _format_prior_intent(current_user_intent: List[str]) -> str:
    lines = [str(line or "").strip() for line in current_user_intent]
    lines = [line for line in lines if line]
    if not lines:
        return "(empty)"
    return "\n".join(f"- {line}" for line in lines)


def _build_user_prompt(
    *,
    tool_name: str,
    tool_arguments: str,
    tool_output: str,
    prior_intent: List[str],
    max_tool_output_chars: int,
) -> str:
    args_text = truncate_text(str(tool_arguments or ""), max_tool_output_chars)
    output_text = truncate_text(str(tool_output or ""), max_tool_output_chars)
    return (
        f"tool_name: {tool_name}\n\n"
        f"tool_arguments:\n{args_text}\n\n"
        f"tool_output:\n{output_text}\n\n"
        f"prior_intent:\n{_format_prior_intent(prior_intent)}\n"
    )


def summarize_tool_intent_line(
    *,
    ctx: Any,
    state: Any,
    tool_name: str,
    tool_arguments: str,
    tool_output_raw: str,
    logger: Any | None = None,
) -> str:
    """
    LLM extraction of one intent line from raw tool output. Fail-open: returns '' on error.
    """
    log = logger if logger is not None else _logger
    scope_cfg = _scope_cfg(ctx)
    output_stripped = str(tool_output_raw or "").strip()
    if not output_stripped:
        return ""

    log_intent_scope_summary_start(
        log, tool_name=tool_name, output_chars=len(output_stripped)
    )

    prior = list(getattr(state, "current_user_intent", []) or [])
    request_messages = [
        {"role": "system", "content": _load_system_prompt(ctx)},
        {
            "role": "user",
            "content": _build_user_prompt(
                tool_name=tool_name,
                tool_arguments=tool_arguments,
                tool_output=output_stripped,
                prior_intent=prior,
                max_tool_output_chars=scope_cfg.max_tool_output_chars,
            ),
        },
    ]
    request_label = f"intent_scope:{tool_name}"
    try:
        raw = request_summary_text(
            ctx=ctx,
            state=state,
            messages=request_messages,
            request_label=request_label,
            payload_overrides=build_intent_scope_payload_overrides(ctx),
        )
    except Exception as exc:
        log.warning(
            "intent_scope summary_failed tool=%s error=%s",
            tool_name,
            exc,
            exc_info=True,
        )
        return ""

    log_llm_prompt_and_result(
        logger=log,
        label=request_label,
        messages=request_messages,
        output_text=raw,
    )

    line = str(raw or "").strip()
    if line and scope_cfg.max_summary_chars > 0:
        line = truncate_text(line, scope_cfg.max_summary_chars)
    log_intent_scope_summary_done(
        log, tool_name=tool_name, summary_chars=len(line)
    )
    return line


def build_fallback_raw_line(
    *,
    ctx: Any,
    tool_output_raw: str,
) -> str:
    scope_cfg = _scope_cfg(ctx)
    body = str(tool_output_raw or "").strip()
    if not body:
        return ""
    prefix = "[intent_scope:raw] "
    max_chars = scope_cfg.max_summary_chars
    if max_chars > 0:
        body = truncate_text(body, max(1, max_chars - len(prefix)))
    return prefix + body
