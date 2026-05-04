from __future__ import annotations

import functools
import inspect
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config_modules.security import validate_command_moderation_mode
from security.path_policy import (
    ToolSecurityContext,
    audit_policy_decision,
    resolve_tool_context,
)

SAFE_VERDICT = "safe"
DANGEROUS_VERDICT = "potentially_dangerous"
ALLOWED_VERDICTS = {SAFE_VERDICT, DANGEROUS_VERDICT}
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def effective_command_cwd(
    tool_name: str,
    kwargs: dict[str, Any],
    context: ToolSecurityContext,
) -> str:
    """
    Canonical working directory shown to the command judge and used by tools that honour it.

    Mirrors default cwd resolution for moderated tools so the judge matches subprocess execution.
    """
    cwd_arg = kwargs.get("cwd")
    if isinstance(cwd_arg, str) and cwd_arg.strip():
        return os.path.abspath(cwd_arg.strip())
    if tool_name == "run_python_code":
        return os.path.abspath(str(context.launch_root))
    return os.path.abspath(os.getcwd())


_SECURITY_PROMPTS_DIR = _PROJECT_ROOT / "prompts" / "security"
_COMMAND_JUDGE_SYSTEM_PROMPT_PATH = _SECURITY_PROMPTS_DIR / "command_judge_system.md"
_COMMAND_JUDGE_USER_PROMPT_PATH = _SECURITY_PROMPTS_DIR / "command_judge_user.md"


@dataclass(frozen=True)
class ModerationContext:
    tool_name: str
    launch_root: str
    extra_write_paths: list[str] = field(default_factory=list)
    mode: str = "deny"
    cwd: str = field(default_factory=os.getcwd)
    os_name: str = field(default_factory=platform.system)
    shell: str = ""
    has_venv: bool = False
    allowed_command_examples: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class JudgeResult:
    verdict: str
    reasons: list[str]
    risk_flags: list[str]
    confidence: float


@dataclass(frozen=True)
class ExecutionDecision:
    allowed: bool
    decision: str
    reason_code: str
    reason: str
    risk_flags: list[str] = field(default_factory=list)
    error_type: str | None = None
    requires_confirmation: bool = False


class JudgeUnavailable(RuntimeError):
    pass


def build_judge_payload(
    tool_name: str,
    payload: str,
    context: ModerationContext,
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "payload": payload,
        "launch_root": context.launch_root,
        "allowed_write_paths": [
            context.launch_root,
            *context.extra_write_paths,
        ],
        "runtime": {
            "cwd": context.cwd,
            "os": context.os_name,
            "shell": context.shell,
            "has_venv": context.has_venv,
        },
        "allowed_command_examples": context.allowed_command_examples,
    }


def run_command_judge(judge_client: Any, judge_payload: dict[str, Any]) -> JudgeResult:
    if judge_client is None:
        raise JudgeUnavailable("judge client is not configured")
    raw_result = judge_client(judge_payload) if callable(judge_client) else judge_client
    if isinstance(raw_result, JudgeResult):
        return raw_result
    return validate_judge_result(raw_result)


def validate_judge_result(raw: dict[str, Any]) -> JudgeResult:
    if not isinstance(raw, dict):
        raise ValueError("judge result must be an object")
    verdict = str(raw.get("verdict", "")).strip().lower()
    if verdict not in ALLOWED_VERDICTS:
        raise ValueError("judge result verdict must be 'safe' or 'potentially_dangerous'")
    reasons = _string_list(raw.get("reasons"), field_name="reasons")
    risk_flags = _string_list(raw.get("risk_flags"), field_name="risk_flags")
    if verdict == DANGEROUS_VERDICT and not risk_flags:
        raise ValueError("potentially_dangerous verdict requires at least one risk flag")
    confidence = raw.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)):
        raise ValueError("judge result confidence must be numeric")
    confidence_float = float(confidence)
    if confidence_float < 0.0 or confidence_float > 1.0:
        raise ValueError("judge result confidence must be between 0.0 and 1.0")
    return JudgeResult(
        verdict=verdict,
        reasons=reasons,
        risk_flags=risk_flags,
        confidence=confidence_float,
    )


def decide_command_execution(
    mode: str,
    judge_result: JudgeResult | None,
    judge_available: bool,
    user_confirmation: bool | None,
) -> ExecutionDecision:
    normalized_mode = validate_command_moderation_mode(mode)
    if normalized_mode == "allow":
        return ExecutionDecision(
            allowed=True,
            decision="executed",
            reason_code="policy_allow",
            reason="command_moderation.mode=allow",
        )
    if not judge_available or judge_result is None:
        return ExecutionDecision(
            allowed=True,
            decision="executed",
            reason_code="judge_unavailable",
            reason="Judge is unavailable; fail-open policy allows execution.",
        )
    if judge_result.verdict == SAFE_VERDICT:
        return ExecutionDecision(
            allowed=True,
            decision="executed",
            reason_code="judge_verdict",
            reason=_join_reasons(judge_result.reasons, default="Judge marked payload as safe."),
            risk_flags=judge_result.risk_flags,
        )
    if normalized_mode == "deny":
        return ExecutionDecision(
            allowed=False,
            decision="blocked",
            reason_code="judge_verdict",
            reason=_join_reasons(judge_result.reasons, default="Judge marked payload as dangerous."),
            risk_flags=judge_result.risk_flags,
            error_type="unsafe_command_blocked",
        )
    if user_confirmation is True:
        return ExecutionDecision(
            allowed=True,
            decision="executed",
            reason_code="user_confirmation",
            reason="User confirmed potentially dangerous execution.",
            risk_flags=judge_result.risk_flags,
        )
    if user_confirmation is False:
        return ExecutionDecision(
            allowed=False,
            decision="cancelled",
            reason_code="user_confirmation",
            reason="User cancelled potentially dangerous execution.",
            risk_flags=judge_result.risk_flags,
            error_type="unsafe_command_cancelled_by_user",
        )
    return ExecutionDecision(
        allowed=False,
        decision="asked",
        reason_code="judge_verdict",
        reason=_join_reasons(judge_result.reasons, default="User confirmation is required."),
        risk_flags=judge_result.risk_flags,
        requires_confirmation=True,
    )


def make_block_error(
    decision: ExecutionDecision,
    payload: str,
    tool_name: str,
    mode: str,
) -> dict[str, Any]:
    preview_key = "code_preview" if tool_name == "run_python_code" else "command_preview"
    error = {
        "type": decision.error_type or "unsafe_command_blocked",
        "mode": mode,
        "reason": decision.reason,
        "risk_flags": decision.risk_flags,
        preview_key: _payload_preview(payload),
    }
    return {key: value for key, value in error.items() if value is not None}


def moderate_command_execution(
    payload_arg: str | None = None,
    payload_extractor: Callable[..., str] | None = None,
    tool_name: str = "",
    judge_client_extractor: Callable[..., Any] | None = None,
    user_confirmation_extractor: Callable[..., bool | None] | None = None,
    blocked_result_factory: Callable[[dict[str, Any]], str] | None = None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            context = resolve_tool_context(*args, **kwargs)
            try:
                payload = _extract_payload(func, args, kwargs, payload_arg, payload_extractor)
            except ValueError:
                return func(*args, **kwargs)
            moderation_context = _build_context(tool_name or func.__name__, context, kwargs)
            if moderation_context.mode == "allow":
                audit_policy_decision(
                    context,
                    tool_name=moderation_context.tool_name,
                    decision="executed",
                    reason_code="policy_allow",
                )
                return func(*args, **kwargs)

            judge_result = None
            judge_available = True
            try:
                judge_client = (
                    judge_client_extractor(*args, **kwargs)
                    if judge_client_extractor is not None
                    else _default_judge_client(args, kwargs)
                )
                judge_result = run_command_judge(
                    judge_client,
                    build_judge_payload(moderation_context.tool_name, payload, moderation_context),
                )
            except Exception:
                judge_available = False

            decision = decide_command_execution(
                moderation_context.mode,
                judge_result,
                judge_available,
                None,
            )
            if decision.requires_confirmation:
                audit_policy_decision(
                    context,
                    tool_name=moderation_context.tool_name,
                    decision="asked",
                    reason_code=decision.reason_code,
                    risk_flags=decision.risk_flags,
                    error_type=decision.error_type,
                )
                user_confirmation = (
                    user_confirmation_extractor(*args, **kwargs)
                    if user_confirmation_extractor is not None
                    else _ask_user_confirmation(payload, moderation_context, decision)
                )
                decision = decide_command_execution(
                    moderation_context.mode,
                    judge_result,
                    judge_available,
                    user_confirmation,
                )
            if decision.allowed:
                audit_policy_decision(
                    context,
                    tool_name=moderation_context.tool_name,
                    decision=decision.decision,
                    reason_code=decision.reason_code,
                    risk_flags=decision.risk_flags,
                    error_type=decision.error_type,
                )
                return func(*args, **kwargs)
            error = make_block_error(
                decision,
                payload,
                moderation_context.tool_name,
                moderation_context.mode,
            )
            audit_policy_decision(
                context,
                tool_name=moderation_context.tool_name,
                decision=decision.decision,
                reason_code=decision.reason_code,
                risk_flags=decision.risk_flags,
                error_type=error.get("type"),
            )
            if blocked_result_factory is not None:
                return blocked_result_factory(error)
            tool_blocked_result = _tool_blocked_result(args)
            if callable(tool_blocked_result):
                return tool_blocked_result(error, *args[1:], **kwargs)
            return json.dumps(
                {
                    "tool": moderation_context.tool_name,
                    "ok": False,
                    "error": error,
                },
                ensure_ascii=False,
            )

        return wrapper

    return decorator


def _build_context(
    tool_name: str,
    context: ToolSecurityContext,
    kwargs: dict[str, Any],
) -> ModerationContext:
    cwd_value = effective_command_cwd(tool_name, kwargs, context)
    return ModerationContext(
        tool_name=tool_name,
        launch_root=context.launch_root,
        extra_write_paths=context.root_box.extra_write_paths,
        mode=context.command_moderation_mode,
        cwd=str(cwd_value),
        os_name=platform.system(),
        shell=os.environ.get("SHELL") or os.environ.get("COMSPEC") or "",
        has_venv=bool(os.environ.get("VIRTUAL_ENV")),
        allowed_command_examples=context.allowed_command_examples,
    )


def _extract_payload(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    payload_arg: str | None,
    payload_extractor: Callable[..., str] | None,
) -> str:
    if payload_extractor is not None:
        return payload_extractor(*args, **kwargs)
    effective_payload_arg = payload_arg or "payload"
    bound = inspect.signature(func).bind_partial(*args, **kwargs)
    payload = bound.arguments.get(effective_payload_arg)
    if not isinstance(payload, str):
        raise ValueError(f"{effective_payload_arg} must be a string")
    return payload


def _string_list(raw: Any, *, field_name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"judge result {field_name} must be a list of strings")
    return raw


def _join_reasons(reasons: list[str], *, default: str) -> str:
    return "; ".join(reason for reason in reasons if reason) or default


def _payload_preview(payload: str, limit: int = 200) -> str:
    compact = " ".join(payload.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit]


def _default_judge_client(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    self_obj = args[0] if args else None
    ctx = kwargs.get("ctx") or getattr(self_obj, "_ctx", None)
    state = kwargs.get("state") or getattr(self_obj, "_state", None)
    cfg = getattr(ctx, "config_dict", None) or getattr(self_obj, "_config_dict", None) or {}
    if ctx is None or state is None or not cfg.get("completions_url"):
        raise JudgeUnavailable("chat/completions context is not configured")

    def judge(judge_payload: dict[str, Any]) -> dict[str, Any]:
        messages = _build_judge_messages(judge_payload)
        from utils.llm_chat_client import request_chat_completion_text

        raw_text = request_chat_completion_text(
            ctx=ctx,
            state=state,
            messages=messages,
            max_tokens=800,
            request_label="command_moderation_judge",
        )
        return _parse_judge_json(raw_text)

    return judge


def _build_judge_messages(judge_payload: dict[str, Any]) -> list[dict[str, str]]:
    system_prompt = load_command_judge_prompt("system")
    user_prompt = load_command_judge_prompt("user").format(
        judge_payload_json=json.dumps(judge_payload, ensure_ascii=False, indent=2)
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def load_command_judge_prompt(kind: str) -> str:
    if kind == "system":
        path = _COMMAND_JUDGE_SYSTEM_PROMPT_PATH
    elif kind == "user":
        path = _COMMAND_JUDGE_USER_PROMPT_PATH
    else:
        raise ValueError("Unknown command judge prompt kind.")
    return _load_prompt_file(path)


def _load_prompt_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise JudgeUnavailable(f"Command judge prompt file is unavailable: {path}") from exc
    if not text:
        raise JudgeUnavailable(f"Command judge prompt file is empty: {path}")
    return text


def _parse_judge_json(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeUnavailable("judge returned invalid JSON") from exc


def _ask_user_confirmation(
    payload: str,
    context: ModerationContext,
    decision: ExecutionDecision,
) -> bool:
    prompt = (
        "\nCommand moderation requires confirmation.\n"
        f"Tool: {context.tool_name}\n"
        f"Reason: {decision.reason}\n"
        f"Risk flags: {', '.join(decision.risk_flags) or '-'}\n"
        f"Payload preview: {_payload_preview(payload)}\n"
        "Type 'yes' to execute: "
    )
    try:
        answer = input(prompt)
    except EOFError:
        print("Command moderation confirmation unavailable; cancelling.", file=sys.stderr)
        return False
    return answer.strip().lower() in {"yes", "y"}


def _tool_blocked_result(args: tuple[Any, ...]) -> Callable[..., str] | None:
    self_obj = args[0] if args else None
    candidate = getattr(self_obj, "_command_moderation_blocked_result", None)
    return candidate if callable(candidate) else None
