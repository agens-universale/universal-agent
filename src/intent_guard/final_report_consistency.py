import json
import os
from typing import Any, Dict, List

from intent_guard.trace_digest import FinishGateTraceDigest, build_finish_gate_trace_digest
from intent_guard.validation_result import (
    ValidationResult,
    fail_open_validation_result,
    parse_validator_response,
)
from summarize.llm import auxiliary_llm_completion_kwargs
from utils.llm_chat_client import log_llm_prompt_and_result, request_chat_completion_text

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_BASE_DIR, "..", ".."))
DEFAULT_INTENT_GUARD_PROMPT_PATH = os.path.abspath(
    os.path.join(_PROJECT_ROOT, "prompts", "chat_completions_intent_guard_prompt.md")
)

_FINISH_GATE_MAX_TOKENS = 1400


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _parse_finish_handoff_args(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return {}
    raw = arguments.strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _load_guard_prompt(prompt_path: str | None = None) -> str:
    path = prompt_path or DEFAULT_INTENT_GUARD_PROMPT_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Intent guard prompt file not found: {path}"
        )
    with open(path, "r", encoding="utf-8") as fd:
        prompt = fd.read().strip()
    if not prompt:
        raise ValueError(
            f"Intent guard prompt file is empty: {path}"
        )
    return prompt


def _wrap_trace_digest_text(trace_digest_text: str) -> str:
    body = (trace_digest_text or "").strip()
    if body.startswith("<dialog_history>"):
        return body
    return f"<dialog_history>\n{body}\n</dialog_history>"


def validate_final_report_vs_user_intent(
    *,
    ctx: Any,
    state: Any,
    current_user_requests: List[str],
    context_user_messages: List[str] | None = None,
    finish_handoff_arguments: Any,
    trace_digest: FinishGateTraceDigest | None = None,
) -> ValidationResult:
    """Run finish gate LLM validator.

    ``current_user_requests`` and ``context_user_messages`` are JSONL lines
    (one ``IntentRecord`` per line, §2.1) via ``records_to_jsonl_lines(deserialize_list(...))``.
    """
    args = _parse_finish_handoff_args(finish_handoff_arguments)
    active_user_requests = [str(x) for x in current_user_requests if isinstance(x, str)]
    user_context = [str(x) for x in (context_user_messages or []) if isinstance(x, str)]

    if trace_digest is None:
        cfg = getattr(ctx, "config_dict", {}) or {}
        trace_digest = build_finish_gate_trace_digest(
            state=state,
            cfg=cfg if isinstance(cfg, dict) else None,
        )

    ctx_cfg = getattr(ctx, "config_dict", {}) or {}
    guard_prompt_path = (
        ctx_cfg.get("chat_completions_intent_guard_prompt_path")
        if isinstance(ctx_cfg, dict)
        else None
    )
    system_prompt = _load_guard_prompt(guard_prompt_path)
    is_subagent = getattr(state, "parent_state_id", None) is not None
    payload = {
        "user_context": user_context,
        "current_user_requests": active_user_requests,
        "trace_digest_text": _wrap_trace_digest_text(trace_digest.trace_digest_text),
        "finish_handoff_arguments": args if args else _to_text(finish_handoff_arguments),
        "agent_kind": "subagent" if is_subagent else "main",
    }
    user_prompt = (
        "Run the two-axis finish gate on the input below.\n"
        "Return ONLY JSON with required keys from the system prompt "
        "(action, trace_ok, report_ok; lists on reject).\n\n"
        f"Input:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    request_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    raw = request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        max_tokens=_FINISH_GATE_MAX_TOKENS,
        request_label="intent_guard_finish_gate",
        payload_overrides=auxiliary_llm_completion_kwargs(ctx),
    )
    log_llm_prompt_and_result(
        logger=getattr(ctx, "logger", None),
        label="intent_guard_finish_gate",
        messages=request_messages,
        output_text=raw,
    )
    parsed = parse_validator_response(raw)
    if parsed is None:
        return fail_open_validation_result()
    return parsed
