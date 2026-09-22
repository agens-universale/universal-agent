from __future__ import annotations

import fnmatch
import functools
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from gc_core.security.dual_wrapper import OffLoop, dual_wrap
from gc_core.security.path_policy import audit_policy_decision, resolve_tool_context


@dataclass(frozen=True)
class UrlPolicyConfig:
    allowed_schemes: list[str] = field(default_factory=lambda: ["http", "https"])
    url_blacklist: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedUrlResult:
    url: str
    scheme: str
    host: str


@dataclass(frozen=True)
class UrlPolicyDecision:
    allowed: bool
    url: str
    error_type: str | None = None
    reason: str | None = None
    matched_rule: str | None = None


def parse_and_validate_url(url: str) -> ParsedUrlResult:
    normalized_url = url.strip() if isinstance(url, str) else ""
    if not normalized_url:
        raise ValueError("URL cannot be empty.")
    parsed = urlsplit(normalized_url)
    if not parsed.scheme:
        raise ValueError("URL must include a scheme.")
    if not parsed.hostname:
        raise ValueError("URL must include a host.")
    return ParsedUrlResult(
        url=normalized_url,
        scheme=parsed.scheme.lower(),
        host=parsed.hostname.lower(),
    )


def is_scheme_allowed(scheme: str, allowed_schemes: list[str]) -> bool:
    normalized_allowed = {item.strip().lower() for item in allowed_schemes if item}
    return scheme.strip().lower() in normalized_allowed


def match_url_blacklist(host: str, url: str, rules: list[str]) -> str | None:
    host_normalized = host.strip().lower()
    url_normalized = url.strip().lower()
    for raw_rule in rules:
        rule = str(raw_rule).strip().lower()
        if not rule:
            continue
        if rule.startswith("*."):
            suffix = rule[2:]
            if host_normalized.endswith(f".{suffix}"):
                return raw_rule
            continue
        if fnmatch.fnmatchcase(host_normalized, rule) or fnmatch.fnmatchcase(
            url_normalized,
            rule,
        ):
            return raw_rule
    return None


def validate_url_policy(
    url: str,
    allowed_schemes: list[str],
    url_blacklist: list[str],
) -> UrlPolicyDecision:
    try:
        parsed = parse_and_validate_url(url)
    except ValueError as exc:
        return UrlPolicyDecision(
            allowed=False,
            url=str(url),
            error_type="url_validation_failed",
            reason=str(exc),
        )
    if not is_scheme_allowed(parsed.scheme, allowed_schemes):
        return UrlPolicyDecision(
            allowed=False,
            url=parsed.url,
            error_type="url_validation_failed",
            reason=f"URL scheme '{parsed.scheme}' is not allowed.",
        )
    matched_rule = match_url_blacklist(parsed.host, parsed.url, url_blacklist)
    if matched_rule is not None:
        return UrlPolicyDecision(
            allowed=False,
            url=parsed.url,
            error_type="url_blocked_by_policy",
            reason="URL host or URL matched blacklist rule.",
            matched_rule=str(matched_rule),
        )
    return UrlPolicyDecision(allowed=True, url=parsed.url)


def enforce_url_policy(
    url_arg: str | None = None,
    url_extractor: Callable[..., str] | None = None,
    blocked_result_factory: Callable[[dict[str, Any]], str] | None = None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        def _flow(*args: Any, **kwargs: Any):
            context = resolve_tool_context(*args, **kwargs)
            if not context.url_policy_enabled:
                return (yield)
            try:
                url = _extract_url(func, args, kwargs, url_arg, url_extractor)
            except ValueError:
                return (yield)
            decision = validate_url_policy(
                url,
                context.url_allowed_schemes,
                context.url_blacklist,
            )
            if decision.allowed:
                audit_policy_decision(
                    context,
                    tool_name=_tool_name(func, args),
                    decision="executed",
                    reason_code="policy_allow",
                )
                return (yield)
            error = {
                "type": decision.error_type,
                "reason": decision.reason,
                "url": decision.url,
            }
            if decision.matched_rule is not None:
                error["matched_rule"] = decision.matched_rule
            mode = (context.url_policy_mode or "deny").strip().lower()
            if mode == "allow":
                audit_policy_decision(
                    context,
                    tool_name=_tool_name(func, args),
                    decision="executed",
                    reason_code="policy_allow_out_of_perimeter",
                    risk_flags=["out_of_perimeter"],
                    error_type=decision.error_type,
                )
                return (yield)
            if mode == "ask":
                from gc_core.security.confirm_port import request_confirm

                # В поток, а не на event loop: режим `ask` здесь — умолчание, то
                # есть обычный путь, а не край.
                confirmed = yield OffLoop(
                    functools.partial(
                        request_confirm,
                        title="Политика URL",
                        question=f"Разрешить запрос вне политики URL: {decision.url}?",
                        fields=[
                            ("Tool", _tool_name(func, args)),
                            ("URL", decision.url),
                            ("Reason", decision.reason or ""),
                            ("Matched rule", decision.matched_rule or "-"),
                        ],
                        ctx=context.ctx,
                        decision_key=f"url:{decision.url}",
                    ),
                    on_interrupt=False,
                )
                if confirmed:
                    audit_policy_decision(
                        context,
                        tool_name=_tool_name(func, args),
                        decision="executed",
                        reason_code="policy_ask_confirmed",
                        risk_flags=["out_of_perimeter"],
                        error_type=decision.error_type,
                    )
                    return (yield)
                error["type"] = "url_blocked_by_policy_cancelled_by_user"
                error["message"] = (
                    f"User declined the request to '{decision.url}'. Do NOT retry the same "
                    "URL, and do NOT silently fetch a different URL — ask the user or stop."
                )
            audit_policy_decision(
                context,
                tool_name=_tool_name(func, args),
                decision="blocked",
                reason_code="policy_deny" if mode == "deny" else "policy_ask_cancelled",
                error_type=error.get("type"),
            )
            if blocked_result_factory is not None:
                return blocked_result_factory(error)
            tool_blocked_result = _tool_blocked_result(args)
            if callable(tool_blocked_result):
                return tool_blocked_result(error, *args[1:], **kwargs)
            return json.dumps(
                {
                    "tool": getattr(func, "__name__", "unknown"),
                    "ok": False,
                    "url": decision.url,
                    "error": error,
                },
                ensure_ascii=False,
            )

        return dual_wrap(func, _flow)

    return decorator


def _extract_url(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    url_arg: str | None,
    url_extractor: Callable[..., str] | None,
) -> str:
    if url_extractor is not None:
        return url_extractor(*args, **kwargs)
    effective_url_arg = url_arg or "url"
    bound = inspect.signature(func).bind_partial(*args, **kwargs)
    url = bound.arguments.get(effective_url_arg)
    if not isinstance(url, str):
        raise ValueError(f"{effective_url_arg} must be a string")
    return url


def _tool_blocked_result(args: tuple[Any, ...]) -> Callable[..., str] | None:
    self_obj = args[0] if args else None
    candidate = getattr(self_obj, "_url_policy_blocked_result", None)
    return candidate if callable(candidate) else None


def _tool_name(func: Callable[..., Any], args: tuple[Any, ...]) -> str:
    self_obj = args[0] if args else None
    description = getattr(self_obj, "tool_description", None)
    if callable(description):
        try:
            spec = description()
            if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                return spec["name"]
        except Exception:
            pass
    return getattr(func, "__name__", "unknown")
