"""Shared chat/completions client helpers for controller-level modules."""

import functools
import json
from typing import Any, Dict, List, Optional

from gc_core.model_router import AliasCircuitOpenError, ResolvedModel
from gc_core.request_backend_policy import arequest_chat_completions_with_policy
from gc_core.runtime_notices import maybe_push_fallback_to_active_notice
from gc_core.usage.account import category_for_request_label
from gc_core.usage.context import usage_scope
from gc_core.utils.backend_errors import is_model_unavailable_error
from gc_core.utils.connect_backend import (
    NO_BACKEND_OVERRIDE,
    acreate_call_headers,
    backend_override_value,
    resolve_network_params,
)

# РУССКИЕ ПОДПИСИ служебных потребителей (спека §5.1/§8) для notice деградации
# «модель привязки недоступна → повтор активной моделью». `auto_judge` в таблице
# нет намеренно: его фоллбэк-цель другая ("default", а не "активная" — см.
# model_router.resolve). Но отсутствие здесь даёт ТОЛЬКО подпись: сама деградация
# ниже применяется ко всем потребителям без разбора, и вызови её auto_judge
# сегодня — ушёл бы на активную модель вопреки этой цели. Пока безвредно (вызовов
# auto_judge ещё нет); каким быть его поведению на мёртвой привязке — решает
# волна C, вместе с появлением самого вызова.
CONSUMER_RU_LABEL = {
    "finish_gate": "судья finish_gate",
    "text_answer_judge": "судья text_answer_judge",
    "command_judge": "судья command_judge",
    "summary": "суммаризация",
    "intent_scope": "intent scope",
    "memory_compaction": "компакция памяти",
}

_PROTECTED_CHAT_COMPLETION_KEYS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "max_tokens",
        "max_completion_tokens",
    }
)


def _positive_int(raw: object, *, default: int) -> int:
    if raw is None or isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        n = int(raw)
        return n if n >= 1 else default
    if isinstance(raw, str):
        return _positive_int_from_str(raw, default)
    return default


def _positive_int_from_str(raw: str, default: int) -> int:
    """Строковая форма: "8192", "32_768", "16k"; пусто/не число/< 1 → default."""
    s = raw.strip().lower().replace(" ", "").replace("_", "")
    if not s:
        return default
    try:
        n = int(float(s[:-1]) * 1000) if s.endswith("k") else int(float(s))
    except ValueError:
        return default
    return n if n >= 1 else default


def _deep_merge_dicts(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _log_completion_response_meta(*, logger: Any, label: str, res: Any) -> None:
    """Полный сырой ответ backend chat/completions."""
    if logger is None:
        return
    try:
        logger.info(
            "chat/completions text-only raw response: label=%s response=%s",
            label,
            json.dumps(res, ensure_ascii=False),
        )
    except Exception:
        logger.info(
            "chat/completions text-only raw response: label=%s response=%r",
            label,
            res,
        )


def shorten_for_log(text: str, max_chars: int = 500) -> str:
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    return f"{s[:max_chars]} ... [TRUNCATED {len(s) - max_chars} chars]"


def log_llm_prompt_and_result(
    *,
    logger: Any,
    label: str,
    messages: List[Dict[str, str]],
    output_text: str,
) -> None:
    if logger is None:
        return
    system_part = "\n\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    )
    user_part = "\n\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "user"
    )
    logger.info(
        "summary_llm_prompt label=%s system=\n%s\n\nuser=\n%s",
        label,
        system_part,
        user_part,
    )
    logger.info("summary_llm_result label=%s output=\n%s", label, output_text or "")


def apply_payload_overrides(
    payload: Dict[str, Any],
    overrides: Optional[Dict[str, Any]],
    *,
    protected: frozenset[str] = _PROTECTED_CHAT_COMPLETION_KEYS,
) -> None:
    """Накладывает per-request kwargs поверх payload (deep merge для вложенных dict)."""
    if not isinstance(overrides, dict) or not overrides:
        return
    for key, value in overrides.items():
        if key in protected:
            continue
        if (
            key in payload
            and isinstance(payload[key], dict)
            and isinstance(value, dict)
        ):
            payload[key] = _deep_merge_dicts(payload[key], value)
        else:
            payload[key] = value


def _build_chat_completion_payload(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    max_tokens: int,
    payload_overrides: Optional[Dict[str, Any]] = None,
    resolved: Optional[ResolvedModel] = None,
) -> Dict[str, Any]:
    cfg = getattr(ctx, "config_dict", {}) or {}
    # tools НЕ отправляем: этот клиент text-only (судья команд, суммаризация,
    # finish-gate, компакция памяти) — инструменты не использует. Пустой
    # "tools": [] — бессмысленный шум; для plain-completion ключ опускаем.
    model = (
        resolved.model
        if resolved is not None
        else (getattr(state, "default_model", None) or cfg.get("model"))
    )
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    use_max_tokens_param = backend_override_value(resolved, "use_max_tokens_param")
    if use_max_tokens_param is NO_BACKEND_OVERRIDE:
        use_max_tokens_param = cfg.get("use_max_tokens_param", False)
    if bool(use_max_tokens_param):
        payload["max_tokens"] = max_tokens
    else:
        payload["max_completion_tokens"] = max_tokens

    extra = backend_override_value(resolved, "chat_completion_kwargs")
    if extra is NO_BACKEND_OVERRIDE:
        extra = cfg.get("chat_completion_kwargs")
    if isinstance(extra, dict) and extra:
        for key, value in extra.items():
            if key in _PROTECTED_CHAT_COMPLETION_KEYS:
                continue
            payload[key] = value

    apply_payload_overrides(payload, payload_overrides)
    return payload


async def _asend_chat_completion_text(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    max_tokens: int,
    request_label: str,
    payload_overrides: Optional[Dict[str, Any]],
    resolved: Optional[ResolvedModel],
) -> str:
    cfg = getattr(ctx, "config_dict", {}) or {}
    log_params = cfg.get("logging_params")
    full_log_backend = (
        bool(log_params.get("full_log_backend", False))
        if isinstance(log_params, dict)
        else False
    )

    def _payload_factory(requested_max_tokens: int) -> Dict[str, Any]:
        return _build_chat_completion_payload(
            ctx=ctx,
            state=state,
            messages=messages,
            max_tokens=requested_max_tokens,
            payload_overrides=payload_overrides,
            resolved=resolved,
        )

    _min_response_tokens_override = backend_override_value(
        resolved, "chat_completions_min_response_tokens"
    )
    min_response_tokens = _positive_int(
        cfg.get("chat_completions_min_response_tokens")
        if _min_response_tokens_override is NO_BACKEND_OVERRIDE
        else _min_response_tokens_override,
        default=1024,
    )
    headers = await acreate_call_headers(state, ctx, resolved=resolved)
    url = resolved.url if resolved is not None else cfg["completions_url"]
    (verify, cert, use_retry, timeout, config_proxy,
     retry_embedded_choice_errors) = resolve_network_params(cfg, resolved)

    usage_category = category_for_request_label(request_label)
    with usage_scope(getattr(state, "usage", None), usage_category, "chat_completions"):
        res, _call_data = await arequest_chat_completions_with_policy(
            payload_factory=_payload_factory,
            initial_max_tokens=max_tokens,
            min_response_tokens=min_response_tokens,
            headers=headers,
            url=url,
            verify=verify,
            cert=cert,
            use_retry=use_retry,
            retry_embedded_choice_errors=retry_embedded_choice_errors,
            salvage_reasoning=False,
            logger=getattr(ctx, "logger", None),
            request_label=request_label,
            runtime_notices_enabled=bool(getattr(ctx, "is_tui", False)),
            notice_url=url,
            timeout=timeout,
            config_proxy=config_proxy,
        )
    logger = getattr(ctx, "logger", None)
    if logger is not None and full_log_backend:
        _log_completion_response_meta(logger=logger, label=request_label, res=res)
    text = (
        res.get("choices", [{}])[0].get("message", {}).get("content")
        if isinstance(res, dict)
        else ""
    )
    return (text or "").strip()


async def arequest_chat_completion_text(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    max_tokens: int,
    consumer: str,
    request_label: str = "chat_completion",
    payload_overrides: Optional[Dict[str, Any]] = None,
) -> str:
    """``consumer`` — canonical id из ``ModelRouter.CONSUMERS`` (спека §5.1); резолвится
    заново на каждый вызов из ``ctx.model_router`` (снимок хода) — не глобально/один раз.

    Класс 1 фоллбэков недоступности (спека §8): если консьюмер резолвится через
    привязку (``assign``) и эта модель недоступна (сетевые/5xx/401/403/404 после
    исчерпания существующих ретраев, или алиас уже открыт в предохранителе), вызов
    повторяется ОДИН РАЗ активной моделью + notice. Если и деградированный вызов
    падает — исключение уходит наверх без изменений (существующая per-consumer
    fail-семантика: finish_gate/text_answer_judge fail-open, summary — пропуск
    цикла, command_judge — fail-closed)."""
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")

    router = getattr(ctx, "model_router", None)
    send = functools.partial(
        _asend_chat_completion_text,
        ctx=ctx,
        state=state,
        messages=messages,
        max_tokens=max_tokens,
        request_label=request_label,
        payload_overrides=payload_overrides,
    )
    if router is None:
        return await send(resolved=None)

    failed_alias: Optional[str] = None
    primary_exc: Optional[Exception] = None
    newly_failed_alias: Optional[str] = None
    try:
        resolved = router.resolve(consumer)
    except AliasCircuitOpenError as exc:
        # Алиас уже известен как мёртвый — сразу к деградации, 0 обращений в транспорт.
        failed_alias = exc.alias
        primary_exc = exc
    else:
        try:
            return await send(resolved=resolved)
        except Exception as exc:
            if resolved.alias is None or not is_model_unavailable_error(exc):
                raise
            failed_alias = resolved.alias
            newly_failed_alias = resolved.alias  # трипнем ПОСЛЕ резолва активной ниже
            primary_exc = exc

    # Определяем активную модель ДО trip() свежего провала: иначе, если консьюмер
    # без привязки уже резолвился в ту же самую активную модель, резолв "main" для
    # сравнения ниже упал бы AliasCircuitOpenError раньше времени.
    active_resolved = _resolve_active_model(router)
    if newly_failed_alias is not None:
        router.breaker.trip(newly_failed_alias, _breaker_reason(primary_exc))

    if active_resolved.alias == failed_alias:
        # Деградировать некуда: привязка и есть активная модель — то же самое
        # упадёт снова. Существующая per-consumer fail-семантика без изменений.
        raise primary_exc

    logger = getattr(ctx, "logger", None)
    who = CONSUMER_RU_LABEL.get(consumer, consumer)
    message = f"{who}: {failed_alias} недоступна — выполнено активной {active_resolved.model}"
    if logger is not None:
        logger.warning(message)
    maybe_push_fallback_to_active_notice(
        who=who,
        failed_alias=failed_alias,
        active_model=active_resolved.model,
        enabled=bool(getattr(ctx, "is_tui", False)),
    )
    try:
        return await send(resolved=active_resolved)
    except Exception as exc:
        if active_resolved.alias is not None and is_model_unavailable_error(exc):
            router.breaker.trip(active_resolved.alias, _breaker_reason(exc))
        raise


def _resolve_active_model(router: Any):
    try:
        return router.resolve("main")
    except AliasCircuitOpenError as open_exc:
        return open_exc.resolved


def _breaker_reason(exc: BaseException) -> str:
    return str(exc).strip()[:300] or exc.__class__.__name__
