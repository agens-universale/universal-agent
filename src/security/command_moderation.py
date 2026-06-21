from __future__ import annotations

import functools
import inspect
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config_modules.security import validate_command_moderation_mode
from constants.tool_names import EXEC_PYTHON_NAME
from security.confirm_port import confirm_available, request_confirm
from security.path_policy import (
    ToolSecurityContext,
    audit_policy_decision,
    resolve_tool_context,
)
from utils.project_data import os_label
from bundled_assets import bundled_root

SAFE_VERDICT = "safe"
DANGEROUS_VERDICT = "potentially_dangerous"
ALLOWED_VERDICTS = {SAFE_VERDICT, DANGEROUS_VERDICT}
_VERDICT_SAFE_RE = re.compile(
    r'"verdict"\s*:\s*"safe"\s*[,}]',
    re.IGNORECASE,
)
_VERDICT_DANGEROUS_RE = re.compile(
    r'"verdict"\s*:\s*"(?:potentially_dangerous|dangerous|unsafe)"\s*[,}]',
    re.IGNORECASE,
)
# bundled_root() — единый резолвер ассетов (editable → repo, wheel → site-packages).
_PROJECT_ROOT = bundled_root()


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
    if tool_name == EXEC_PYTHON_NAME:
        return os.path.abspath(str(context.launch_root))
    return os.path.abspath(os.getcwd())


_SECURITY_PROMPTS_DIR = _PROJECT_ROOT / "prompts" / "security"
_COMMAND_JUDGE_SYSTEM_PROMPT_PATH = _SECURITY_PROMPTS_DIR / "command_judge_system.md"
_COMMAND_JUDGE_USER_PROMPT_PATH = _SECURITY_PROMPTS_DIR / "command_judge_user.md"


@dataclass(frozen=True)
class ModerationContext:
    tool_name: str
    launch_root: str
    extra_paths: list[str] = field(default_factory=list)
    mode: str = "deny"
    cwd: str = field(default_factory=os.getcwd)
    os_name: str = field(default_factory=os_label)
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


def _get_protected_paths(cwd: str | None):
    from security.protected_paths import protected_paths
    return protected_paths(cwd=cwd)


def build_judge_payload(
    tool_name: str,
    payload: str,
    context: ModerationContext,
) -> dict[str, Any]:
    try:
        ps = _get_protected_paths(context.cwd)
        never_touch = sorted(
            str(p) for p in ps.instructions | ps.operational
        )
    except Exception:
        never_touch = []
    return {
        "tool_name": tool_name,
        "payload": payload,
        "launch_root": context.launch_root,
        "allowed_write_paths": [
            context.launch_root,
            *context.extra_paths,
        ],
        "runtime": {
            "cwd": context.cwd,
            "os": context.os_name,
            "shell": context.shell,
            "has_venv": context.has_venv,
        },
        "allowed_command_examples": context.allowed_command_examples,
        "never_touch_paths": never_touch,
    }


def run_command_judge(judge_client: Any, judge_payload: dict[str, Any]) -> JudgeResult:
    if judge_client is None:
        raise JudgeUnavailable("judge client is not configured")
    raw_result = judge_client(judge_payload) if callable(judge_client) else judge_client
    if isinstance(raw_result, JudgeResult):
        return raw_result
    return validate_judge_result(raw_result)


def run_command_judge_with_retry(
    judge_client: Any, judge_payload: dict[str, Any], *, retries: int = 1
) -> JudgeResult:
    """Судья с ретраями: слабая модель часто отдаёт мусорный вердикт разово —
    один повтор перед тем как признать судью недоступным. Если все попытки
    исчерпаны — пробрасываем последнее исключение (декоратор переведёт это в
    judge_unavailable → fail-closed, security не ослаблен)."""
    last_exc: Exception | None = None
    for _ in range(retries + 1):
        try:
            return run_command_judge(judge_client, judge_payload)
        except Exception as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def validate_judge_result(raw: dict[str, Any]) -> JudgeResult:
    if not isinstance(raw, dict):
        raise ValueError("judge result must be an object")
    # Нормализуем ФОРМАТ (пробел/дефис/регистр): слабая модель часто пишет
    # «potentially dangerous» через пробел/в другом регистре. Синонимы НЕ маппим —
    # неузнанный вердикт по-прежнему отвергается (security не ослаблен).
    verdict = re.sub(r"[\s\-]+", "_", str(raw.get("verdict", "")).strip().lower())
    if verdict not in ALLOWED_VERDICTS:
        raise ValueError("judge result verdict must be 'safe' or 'potentially_dangerous'")
    reasons = _string_list(raw.get("reasons"), field_name="reasons")
    risk_flags = _string_list(raw.get("risk_flags"), field_name="risk_flags")
    if verdict == DANGEROUS_VERDICT and not risk_flags:
        raise ValueError("potentially_dangerous verdict requires at least one risk flag")
    confidence_float = _parse_judge_confidence(raw.get("confidence", 0.0))
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
    *,
    ctx: Any = None,
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
        if normalized_mode == "ask":
            if user_confirmation is True:
                return ExecutionDecision(
                    allowed=True,
                    decision="executed",
                    reason_code="user_confirmation",
                    reason="User confirmed execution after judge was unavailable.",
                )
            if user_confirmation is False:
                return ExecutionDecision(
                    allowed=False,
                    decision="cancelled",
                    reason_code="user_confirmation",
                    reason=(
                        "User declined the command. Do NOT retry the same command, "
                        "and do NOT silently run a modified command — ask the user or stop."
                    ),
                    error_type="unsafe_command_cancelled_by_user",
                )
            return ExecutionDecision(
                allowed=False,
                decision="asked",
                reason_code="judge_unavailable",
                reason="Judge is unavailable; user confirmation is required.",
                requires_confirmation=True,
            )
        # C1 Часть 2: при недоступности судьи в deny — пробуем confirm-порт.
        if confirm_available(ctx):
            confirmed = request_confirm(
                title="Command moderation",
                question="Судья недоступен. Выполнить команду?",
                fields=[("mode", normalized_mode)],
                ctx=ctx,
            )
            if confirmed:
                return ExecutionDecision(
                    allowed=True,
                    decision="executed",
                    reason_code="judge_unavailable_user_confirmed",
                    reason="User confirmed execution after judge was unavailable.",
                )
        # Fail-closed: confirm недоступен или пользователь отказал — блокируем.
        return ExecutionDecision(
            allowed=False,
            decision="blocked",
            reason_code="judge_unavailable",
            reason="Judge is unavailable; fail-closed policy blocks execution.",
            error_type="unsafe_command_blocked",
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
            reason=(
                "User declined the command. Do NOT retry the same command, "
                "and do NOT silently run a modified command — ask the user or stop."
            ),
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
    preview_key = "code_preview" if tool_name == EXEC_PYTHON_NAME else "command_preview"
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
            # Если в config_dict отсутствует блок command_moderation вообще —
            # модерация не сконфигурирована, пропускаем (legacy / unit-test поведение,
            # симметрично write_guard.enabled default'у).
            from security.path_policy import _extract_config_dict
            _cfg = _extract_config_dict(
                args[0] if args else None,
                getattr(context, "ctx", None),
                kwargs,
            )
            cm_cfg = _cfg.get("command_moderation")
            if not isinstance(cm_cfg, dict):
                return func(*args, **kwargs)
            # Safe-by-default-OFF: если слой не включён явно — судья не вызывается.
            if not bool(cm_cfg.get("enabled", False)):
                return func(*args, **kwargs)
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
                judge_result = run_command_judge_with_retry(
                    judge_client,
                    build_judge_payload(moderation_context.tool_name, payload, moderation_context),
                    retries=1,
                )
            except Exception as exc:
                judge_available = False
                _log_command_judge_failure(
                    context,
                    tool_name=moderation_context.tool_name,
                    exc=exc,
                )

            tool_ctx = getattr(context, "ctx", None)
            decision = decide_command_execution(
                moderation_context.mode,
                judge_result,
                judge_available,
                None,
                ctx=tool_ctx,
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
                    else _ask_user_confirmation(payload, moderation_context, decision, ctx=tool_ctx)
                )
                decision = decide_command_execution(
                    moderation_context.mode,
                    judge_result,
                    judge_available,
                    user_confirmation,
                    ctx=tool_ctx,
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
        extra_paths=context.write_guard.extra_paths,
        mode=context.command_moderation_mode,
        cwd=str(cwd_value),
        os_name=os_label(),
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


def _log_command_judge_failure(
    context: ToolSecurityContext,
    *,
    tool_name: str,
    exc: BaseException,
) -> None:
    logger = context.logger
    if logger is None or not hasattr(logger, "warning"):
        return
    event = {
        "event": "command_moderation_judge_failed",
        "tool_name": tool_name,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    try:
        logger.warning(
            "command_moderation_judge_failed %s",
            json.dumps(event, ensure_ascii=False, sort_keys=True),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    except Exception:
        return


def _parse_judge_confidence(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        confidence_float = float(raw)
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            raise ValueError("judge result confidence must be numeric")
        try:
            confidence_float = float(stripped)
        except ValueError as exc:
            raise ValueError("judge result confidence must be numeric") from exc
    else:
        raise ValueError("judge result confidence must be numeric")
    if confidence_float < 0.0 or confidence_float > 1.0:
        raise ValueError("judge result confidence must be between 0.0 and 1.0")
    return confidence_float


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
        return _parse_judge_json(
            raw_text,
            logger=getattr(ctx, "logger", None),
            tool_name=str(judge_payload.get("tool_name") or ""),
        )

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


def _strip_judge_response_text(raw_text: str) -> str:
    text = (raw_text or "").strip()
    # Reasoning models (Minimax, DeepSeek-R1 и т.п.) могут встраивать thinking-блок в content.
    for pattern in (
        r"<think\b[^>]*>.*?</think>",
        r"<redacted_thinking\b[^>]*>.*?</think>",
    ):
        text = re.sub(pattern, "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return text


def _extract_json_candidate(text: str) -> str:
    if text.startswith("{"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _verdict_regex_fallback(text: str) -> dict[str, Any] | None:
    """
    Last-resort parser: ищем явное поле verdict в ответе модели.
    Dangerous проверяем раньше safe (fail-closed при конфликте).
    """
    if _VERDICT_DANGEROUS_RE.search(text):
        return {
            "verdict": DANGEROUS_VERDICT,
            "reasons": ["Judge response parsed via regex fallback."],
            "risk_flags": ["judge_response_malformed"],
            "confidence": 0.0,
        }
    if _VERDICT_SAFE_RE.search(text):
        return {
            "verdict": SAFE_VERDICT,
            "reasons": ["Judge response parsed via regex fallback."],
            "risk_flags": [],
            "confidence": 0.0,
        }
    return None


def _log_command_judge_regex_fallback(
    logger: Any,
    *,
    tool_name: str | None,
    verdict: str,
) -> None:
    if logger is None or not hasattr(logger, "warning"):
        return
    event = {
        "event": "command_moderation_judge_regex_fallback",
        "tool_name": tool_name or "",
        "matched_verdict": verdict,
    }
    try:
        logger.warning(
            "command_moderation_judge_regex_fallback %s",
            json.dumps(event, ensure_ascii=False, sort_keys=True),
        )
    except Exception:
        return


def _parse_judge_json(
    raw_text: str,
    *,
    logger: Any = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    text = _strip_judge_response_text(raw_text)
    if not text:
        raise JudgeUnavailable("judge returned empty content")
    json_candidate = _extract_json_candidate(text)
    try:
        return json.loads(json_candidate)
    except json.JSONDecodeError:
        fallback = _verdict_regex_fallback(text)
        if fallback is None:
            raise JudgeUnavailable("judge returned unparseable response")
        _log_command_judge_regex_fallback(
            logger,
            tool_name=tool_name,
            verdict=str(fallback["verdict"]),
        )
        return fallback


def _ask_user_confirmation(
    payload: str,
    context: ModerationContext,
    decision: ExecutionDecision,
    ctx: Any = None,
) -> bool:
    return request_confirm(
        title="Command moderation",
        question=f"Разрешить выполнение команды? ({context.tool_name})",
        fields=[
            ("Tool", context.tool_name),
            ("Reason", decision.reason),
            ("Risk flags", ", ".join(decision.risk_flags) or "-"),
            ("Preview", _payload_preview(payload)),
        ],
        ctx=ctx,
        decision_key=f"command:{payload}",
    )


def _tool_blocked_result(args: tuple[Any, ...]) -> Callable[..., str] | None:
    self_obj = args[0] if args else None
    candidate = getattr(self_obj, "_command_moderation_blocked_result", None)
    return candidate if callable(candidate) else None
