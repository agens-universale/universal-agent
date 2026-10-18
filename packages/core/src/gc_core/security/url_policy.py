from __future__ import annotations

import fnmatch
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from gc_core.message_policy.permission_denials import (
    OTHER_HOST_REDIRECT_TYPE,
    other_host_redirect_reason,
)
from gc_core.security.confirm_port import confirmation_step, refuse_in_place
from gc_core.security.dual_wrapper import dual_wrap
from gc_core.security.mode_stage import (
    ModeAction,
    remembered_denial,
    resolve_by_mode,
)
from gc_core.security.path_policy import (
    audit_policy_decision,
    resolve_tool_context,
)
from gc_core.security.permission_mode import (
    ASK,
    CAT_NETWORK,
    PASS,
    network_decision,
)


@dataclass(frozen=True)
class UrlPolicyConfig:
    allowed_schemes: list[str] = field(
        default_factory=lambda: ["http", "https"]
    )
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
    normalized_allowed = {
        item.strip().lower() for item in allowed_schemes if item
    }
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


def validate_redirect_hop(
    current: str,
    location: str,
    allowed_schemes: list[str],
    url_blacklist: list[str],
    *,
    same_host_only: bool = False,
) -> UrlPolicyDecision:
    """Решение по переходу редиректа: политика адресов, затем хост.

    ``same_host_only`` — режим не пускает переход на другой хост
    (``permission_mode.follows_cross_host_redirects``): такой переход не
    выполняется, модель получает адрес и запрашивает его отдельно. Хост —
    тот же ключ, под которым человек отвечает про сайт (`url_host_key`):
    смена схемы и порта хоста не меняет.
    """
    decision = validate_url_policy(location, allowed_schemes, url_blacklist)
    if not decision.allowed or not same_host_only:
        return decision
    if url_host_key(decision.url) == url_host_key(current):
        return decision
    return UrlPolicyDecision(
        allowed=False,
        url=decision.url,
        error_type=OTHER_HOST_REDIRECT_TYPE,
        reason=other_host_redirect_reason(decision.url),
    )


def _url_policy_flow(func, context, url, args, kwargs, blocked):
    """Политика адресов после стадии режима: перечень разрешённого, явный
    режим политики и вопрос про адрес вне её."""
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
        confirmed = yield confirmation_step(
            local_request=request_confirm,
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
        refuse_in_place(
            error,
            context.ctx,
            "url_blocked_by_policy_cancelled_by_user",
            url=decision.url,
        )
    audit_policy_decision(
        context,
        tool_name=_tool_name(func, args),
        decision="blocked",
        reason_code="policy_deny"
        if mode == "deny"
        else "policy_ask_cancelled",
        error_type=error.get("type"),
    )
    return blocked(error, decision.url, args, kwargs)


def _url_blocked_result(
    func, blocked_result_factory, error: dict[str, Any], url: str, args, kwargs
) -> str:
    """Ответ тула на заблокированный запрос: фабрика декоратора, своя форма
    тула или общий конверт."""
    if blocked_result_factory is not None:
        return blocked_result_factory(error)
    tool_blocked_result = _tool_blocked_result(args)
    if callable(tool_blocked_result):
        return tool_blocked_result(error, *args[1:], **kwargs)
    return json.dumps(
        {
            "tool": getattr(func, "__name__", "unknown"),
            "ok": False,
            "url": url,
            "error": error,
        },
        ensure_ascii=False,
    )


def enforce_url_policy(
    url_arg: str | None = None,
    url_extractor: Callable[..., str] | None = None,
    blocked_result_factory: Callable[[dict[str, Any]], str] | None = None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        def _blocked(error: dict[str, Any], url: str, args, kwargs) -> str:
            return _url_blocked_result(
                func, blocked_result_factory, error, url, args, kwargs
            )

        def _flow(*args: Any, **kwargs: Any):
            context = resolve_tool_context(*args, **kwargs)
            try:
                url = _extract_url(func, args, kwargs, url_arg, url_extractor)
            except ValueError:
                return (yield)
            # Стадия режима разрешений — ДО `url_policy.enabled`: выключенная
            # в конфиге политика режим не отменяет.
            call_now, error = yield from _mode_network_stage(
                url, context, _tool_name(func, args)
            )
            if error is not None:
                return _blocked(error, url, args, kwargs)
            if call_now or not context.url_policy_enabled:
                return (yield)
            return (
                yield from _url_policy_flow(
                    func, context, url, args, kwargs, _blocked
                )
            )

        return dual_wrap(func, _flow)

    return decorator


#: Префикс ключа решения про сайт.
_HOST_KEY_PREFIX = "host:"


def url_host_key(url: str) -> str:
    """Ключ решения про сайт: ответ человека «в сессии» на сетевой запрос
    запоминается для хоста целиком, а не для точного адреса — иначе каждая
    страница того же сайта спрашивала бы заново."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    return f"{_HOST_KEY_PREFIX}{host}"


def _normalized_url(url: str) -> str:
    try:
        return parse_and_validate_url(url).url
    except ValueError:
        return str(url).strip()


#: Сервисы метаданных облака — единственные внутренние адреса, обращение к
#: которым судья в auto считает рискованным само по себе (строгая пометка):
#: оттуда берут учётные данные машины. Так у эталона; локальные и частные
#: адреса — обычный запрос, опасное там ловят обычные правила судьи.
CLOUD_METADATA_HOSTS = (
    "169.254.169.254",
    "metadata.google.internal",
    "*.metadata.google.internal",
    "metadata.azure.com",
    "*.metadata.azure.com",
)


def _is_cloud_metadata(url: str, allowed_schemes) -> bool:
    return (
        validate_url_policy(
            url, allowed_schemes, list(CLOUD_METADATA_HOSTS)
        ).matched_rule
        is not None
    )


def _mode_network_stage(url: str, context: Any, tool_name: str):
    """Стадия режима разрешений для сетевого запроса: ``(call_now, error)``.

    ``error`` — тело отказа; ``call_now`` — запрос разрешён здесь и сейчас,
    политика адресов ниже уже учтена; оба пусты — режим ничего не добавил,
    решает политика адресов, как раньше.

    Порядок: запомненный запрет человека (сайт, адрес) → адрес, не прошедший
    политику → вопрос режима про сайт.
    """
    ctx = context.ctx
    # Ключ адреса — тот же, под которым ответ записывает политика адресов
    # ниже: по нормализованному адресу, иначе пробел в начале обходил бы
    # запомненный запрет.
    url_key = f"url:{_normalized_url(url)}"
    host_key = url_host_key(url)
    host = host_key.removeprefix(_HOST_KEY_PREFIX)
    if not host:
        # Адрес без хоста (`file:///…`): разрешение «сайта» покрыло бы любой
        # такой адрес — ключ остаётся по адресу.
        host_key = url_key

    # Запомненный запрет сайта сильнее режима — в любом режиме.
    denied = remembered_denial(context, tool_name, host_key, url=url)
    if denied is not None:
        return False, denied
    mode_decision = network_decision(ctx)
    if mode_decision == PASS:
        # Запрет точного адреса здесь помнит сама политика адресов и отвечает
        # своим конвертом — как раньше.
        return False, None
    denied = remembered_denial(context, tool_name, url_key, url=url)
    if denied is not None:
        return False, denied

    # Схема и форма адреса проверяются и при выключенной политике: это не
    # вопрос намерений. Чёрный список — только при включённой.
    policy = validate_url_policy(
        url,
        context.url_allowed_schemes,
        context.url_blacklist if context.url_policy_enabled else [],
    )
    explicit = (context.url_policy_mode or "deny").strip().lower()
    key, flags, error = _network_judgement(
        mode_decision, url, url_key, host_key, policy, explicit, context
    )
    if error is not None:
        audit_policy_decision(
            context,
            tool_name=tool_name,
            decision="blocked",
            reason_code="policy_deny",
            error_type=policy.error_type,
        )
        return False, error
    if key is None:
        return False, None
    allowed, error = yield from resolve_by_mode(
        mode_decision,
        ModeAction(
            context=context,
            tool_name=tool_name,
            category=CAT_NETWORK,
            decision_key=key,
            question=(
                "Сетевой запрос",
                f"Разрешить запрос к {host or url}?",
                [("Tool", tool_name), ("URL", url)],
            ),
            flags=flags,
            error_fields={"url": url},
            deny_type="network_denied_by_mode",
            deny_reason=(
                "Network request not permitted in the current permission mode."
            ),
        ),
    )
    return (True, None) if allowed else (False, error)


def _network_judgement(
    mode_decision, url, url_key, host_key, policy, explicit, context
):
    """Ключ решения и пометки судьи для сетевого запроса с учётом политики
    адресов: ``(ключ, пометки, отказ)``. Ключ None — решает сама политика.
    """
    from gc_core.security.action_judge import FLAG_RESTRICTED_ADDRESS

    violation = not policy.allowed
    if mode_decision == ASK:
        if violation and context.url_policy_enabled and explicit != "allow":
            # Адрес, не прошедший политику, решает она сама: спрашивает по
            # своему, более строгому ключу, или отказывает. Вопрос про сайт
            # не дублируется. При явном `allow` политика пропустила бы адрес
            # молча — тогда спрашивает режим, как про любой запрос.
            return None, (), None
        return host_key, (), None
    # ALIGN — решает судья действий.
    key = host_key
    if violation:
        if policy.matched_rule is None or explicit == "deny":
            # Неразрешённая схема и негодный адрес — не вопрос намерений;
            # явный `deny` сильнее судьи.
            error = {
                "type": policy.error_type,
                "reason": policy.reason,
                "url": policy.url,
            }
            if policy.matched_rule is not None:
                error["matched_rule"] = policy.matched_rule
            return None, (), error
        # Адрес из чёрного списка: разрешение сайта его не покрывает — ключ
        # по точному адресу.
        key = url_key
    flags: tuple[str, ...] = ()
    if explicit != "allow" and _is_cloud_metadata(
        url, context.url_allowed_schemes
    ):
        # Явный `allow` политики адресов снимает пометку.
        flags = (FLAG_RESTRICTED_ADDRESS,)
    return key, flags, None


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
