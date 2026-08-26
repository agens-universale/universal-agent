import json
import os
from typing import Any, Dict, List

from gc_core.intent_guard.trace_digest import FinishGateTraceDigest, build_finish_gate_trace_digest
from gc_core.intent_guard.validation_result import ValidationResult, fail_open_validation_result, parse_validator_response
from gc_core.summarize.llm import auxiliary_llm_completion_kwargs, auxiliary_llm_judge_max_tokens, request_auxiliary_completion_text
from gc_core.bundled_assets import bundled_root

# bundled_root() — единый резолвер ассетов (editable → repo, wheel → site-packages).
_PROJECT_ROOT = str(bundled_root())
DEFAULT_INTENT_GUARD_PROMPT_PATH = os.path.abspath(
    os.path.join(_PROJECT_ROOT, "prompts", "chat_completions_intent_guard_prompt.md")
)

_FINISH_GATE_RETRY_DRAFT_MAX_CHARS = 6000


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


def _normalize_handoff_in_finish_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Decode string-typed ``handoff`` to object for the finish-gate LLM payload.

    Tool schema requires ``handoff`` as a JSON string; tool outputs / digest already
    expose a parsed object. Normalize so Axis B compares object↔object.
    """
    if not isinstance(args, dict):
        return {}
    out = dict(args)
    if "handoff" not in out:
        return out
    raw = out["handoff"]
    if isinstance(raw, dict):
        return out
    if not isinstance(raw, str):
        return out
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return out
    if isinstance(parsed, dict):
        out["handoff"] = parsed
    return out


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


def _clip_retry_draft(raw: str, *, max_chars: int = _FINISH_GATE_RETRY_DRAFT_MAX_CHARS) -> str:
    text = (raw or "").strip()
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return (
        text[:head]
        + "\n... [TRUNCATED FOR RETRY PROMPT] ...\n"
        + text[-tail:]
    )


def _build_finish_gate_user_prompt(payload: Dict[str, Any]) -> str:
    return (
        "Run the two-axis finish gate on the input below.\n"
        "Do NOT reason out loud. Do NOT write <think> or analysis prose.\n"
        "Return ONLY a single raw JSON object with required keys from the system prompt "
        "(action, trace_ok, report_ok; lists on reject).\n\n"
        f"Input:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _build_finish_gate_retry_user_prompt(
    *,
    payload: Dict[str, Any],
    previous_raw: str,
) -> str:
    # previous_raw already projected by aux LLM choke.
    draft = _clip_retry_draft(previous_raw)
    return (
        "Your previous finish-gate reply was INVALID: it was not parseable JSON with "
        "required keys (action, trace_ok, report_ok), or it was truncated mid-reasoning.\n\n"
        "STRICT RETRY RULES:\n"
        "- Do NOT reason. Do NOT write <think>…</think>, commentary, or step-by-step text.\n"
        "- Return ONLY the raw JSON verdict object (no markdown fences, no preamble).\n"
        "- You may use your previous incomplete output below as scratch notes, then emit JSON only.\n\n"
        "Previous incomplete output:\n"
        "-----\n"
        f"{draft}\n"
        "-----\n\n"
        "Original input (unchanged):\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _request_finish_gate_once(
    *,
    ctx: Any,
    state: Any,
    system_prompt: str,
    user_prompt: str,
    request_label: str,
    consumer: str,
) -> str:
    request_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return request_auxiliary_completion_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        max_tokens=auxiliary_llm_judge_max_tokens(ctx),
        request_label=request_label,
        consumer=consumer,
        payload_overrides=auxiliary_llm_completion_kwargs(ctx),
    )


def validate_final_report_vs_user_intent(
    *,
    ctx: Any,
    state: Any,
    current_user_requests: List[str],
    context_user_messages: List[str] | None = None,
    finish_handoff_arguments: Any,
    trace_digest: FinishGateTraceDigest | None = None,
    consumer: str = "finish_gate",
) -> ValidationResult:
    """Run finish gate LLM validator.

    ``current_user_requests`` and ``context_user_messages`` are JSONL lines
    (one ``IntentRecord`` per line, §2.1) via ``records_to_jsonl_lines(deserialize_list(...))``.

    On unparseable / truncated first reply, retries once with a stricter JSON-only
    prompt that includes the previous incomplete output. Still fail-open if retry fails.

    ``consumer`` — canonical id этого судейского вызова (спека §5.1). По умолчанию
    ``"finish_gate"``; ``text_answer_judge`` переиспользует эту же воронку под своим id.
    """
    args = _normalize_handoff_in_finish_args(
        _parse_finish_handoff_args(finish_handoff_arguments)
    )
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

    raw = _request_finish_gate_once(
        ctx=ctx,
        state=state,
        system_prompt=system_prompt,
        user_prompt=_build_finish_gate_user_prompt(payload),
        request_label="intent_guard_finish_gate",
        consumer=consumer,
    )
    parsed = parse_validator_response(raw)
    if parsed is not None:
        return parsed

    logger = getattr(ctx, "logger", None)
    if logger is not None:
        logger.warning(
            "finish_gate first reply unparseable; retrying with JSON-only prompt "
            "previous_chars=%s",
            len(raw or ""),
        )

    raw_retry = _request_finish_gate_once(
        ctx=ctx,
        state=state,
        system_prompt=system_prompt,
        user_prompt=_build_finish_gate_retry_user_prompt(
            payload=payload,
            previous_raw=raw,
        ),
        request_label="intent_guard_finish_gate_retry",
        consumer=consumer,
    )
    parsed_retry = parse_validator_response(raw_retry)
    if parsed_retry is not None:
        return parsed_retry
    return fail_open_validation_result()
