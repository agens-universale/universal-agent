import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List

from utils.llm_chat_client import log_llm_prompt_and_result, request_chat_completion_text
from utils.json_text import parse_json_from_text

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_BASE_DIR, "..", ".."))
DEFAULT_INTENT_GUARD_PROMPT_PATH = os.path.abspath(
    os.path.join(_PROJECT_ROOT, "prompts", "chat_completions_intent_guard_prompt.md")
)


@dataclass
class ValidationResult:
    ok: bool
    missing_requirements: List[str]
    conflicts: List[str]
    coverage_score: float
    feedback_for_agent: str


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _parse_submit_solution_args(arguments: Any) -> Dict[str, Any]:
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


def _load_guard_prompt(ctx: Any) -> str:
    cfg = getattr(ctx, "config_dict", {}) or {}
    summary_params = (
        cfg.get("summary_params") if isinstance(cfg.get("summary_params"), dict) else {}
    )
    prompt_path_raw = str(
        summary_params.get("chat_completions_intent_guard_prompt_path")
        or DEFAULT_INTENT_GUARD_PROMPT_PATH
    ).strip()
    if not prompt_path_raw:
        raise ValueError(
            "summary_params.chat_completions_intent_guard_prompt_path is empty"
        )
    prompt_path = (
        prompt_path_raw
        if os.path.isabs(prompt_path_raw)
        else os.path.abspath(os.path.join(_PROJECT_ROOT, prompt_path_raw))
    )
    if not os.path.exists(prompt_path):
        raise FileNotFoundError(f"Intent guard prompt file not found: {prompt_path}")
    with open(prompt_path, "r", encoding="utf-8") as fd:
        prompt = fd.read().strip()
    if not prompt:
        raise ValueError(f"Intent guard prompt file is empty: {prompt_path}")
    return prompt


def validate_final_report_vs_user_intent(
    *,
    ctx: Any,
    state: Any,
    user_messages_log: List[str],
    submit_solution_arguments: Any,
) -> ValidationResult:
    args = _parse_submit_solution_args(submit_solution_arguments)

    system_prompt = _load_guard_prompt(ctx)
    payload = {
        "user_messages_log": [str(x) for x in user_messages_log if isinstance(x, str)],
        "submit_solution_arguments": args if args else _to_text(submit_solution_arguments),
    }
    user_prompt = (
        "Check final-report consistency against initial user intent.\n\n"
        "Return ONLY JSON object with keys:\n"
        "{\"ok\": bool, \"coverage_score\": number, \"conflicts\": [str], "
        "\"missing_requirements\": [str], \"feedback_for_agent\": str}\n\n"
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
        max_tokens=1200,
        request_label="intent_guard_consistency",
    )
    log_llm_prompt_and_result(
        logger=getattr(ctx, "logger", None),
        label="intent_guard_consistency",
        messages=request_messages,
        output_text=raw,
    )
    parsed = parse_json_from_text(raw, default=None)
    if parsed is None:
        return ValidationResult(
            ok=True,
            missing_requirements=[],
            conflicts=[],
            coverage_score=1.0,
            feedback_for_agent="",
        )
    if not isinstance(parsed, dict):
        return ValidationResult(
            ok=True,
            missing_requirements=[],
            conflicts=[],
            coverage_score=1.0,
            feedback_for_agent="",
        )
    ok = bool(parsed.get("ok", False))
    missing = parsed.get("missing_requirements")
    conflicts = parsed.get("conflicts")
    coverage_raw = parsed.get("coverage_score", 0.0)
    feedback = str(parsed.get("feedback_for_agent") or "").strip()
    try:
        coverage = float(coverage_raw)
    except (TypeError, ValueError):
        coverage = 0.0
    if not isinstance(missing, list):
        missing = []
    if not isinstance(conflicts, list):
        conflicts = []
    if not feedback and not ok:
        feedback = (
            "submit_solution rejected by finish gate.\n"
            "Final report is not consistent with initial user intent. Continue execution unless"
            " it is explicitly impossible to make further progress."
        )
    return ValidationResult(
        ok=ok,
        missing_requirements=[str(x) for x in missing if x is not None],
        conflicts=[str(x) for x in conflicts if x is not None],
        coverage_score=coverage,
        feedback_for_agent=feedback,
    )
