"""Shared chat/completions client helpers for controller-level modules."""

import json
from typing import Any, Dict, List, Optional

from request_backend_policy import request_chat_completions_with_policy
from utils.connect_backend import create_call_headers

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
        s = raw.strip().lower().replace(" ", "").replace("_", "")
        if not s:
            return default
        if s.endswith("k"):
            try:
                n = int(float(s[:-1]) * 1000)
            except ValueError:
                return default
            return n if n >= 1 else default
        try:
            n = int(float(s))
        except ValueError:
            return default
        return n if n >= 1 else default
    return default


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
) -> Dict[str, Any]:
    cfg = getattr(ctx, "config_dict", {}) or {}
    payload: Dict[str, Any] = {
        "model": getattr(state, "default_model", None) or cfg.get("model"),
        "messages": messages,
        "tools": [],
    }
    if bool(cfg.get("use_max_tokens_param", False)):
        payload["max_tokens"] = max_tokens
    else:
        payload["max_completion_tokens"] = max_tokens

    extra = cfg.get("chat_completion_kwargs")
    if isinstance(extra, dict) and extra:
        for key, value in extra.items():
            if key in _PROTECTED_CHAT_COMPLETION_KEYS:
                continue
            payload[key] = value

    apply_payload_overrides(payload, payload_overrides)
    return payload


def request_chat_completion_text(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    max_tokens: int,
    request_label: str = "chat_completion",
    payload_overrides: Optional[Dict[str, Any]] = None,
) -> str:
    cfg = getattr(ctx, "config_dict", {}) or {}
    log_params = cfg.get("logging_params")
    full_log_backend = (
        bool(log_params.get("full_log_backend", False))
        if isinstance(log_params, dict)
        else False
    )
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")

    def _payload_factory(requested_max_tokens: int) -> Dict[str, Any]:
        return _build_chat_completion_payload(
            ctx=ctx,
            state=state,
            messages=messages,
            max_tokens=requested_max_tokens,
            payload_overrides=payload_overrides,
        )

    min_response_tokens = _positive_int(
        cfg.get("chat_completions_min_response_tokens"),
        default=1024,
    )
    headers = create_call_headers(state, ctx)
    res, _call_data = request_chat_completions_with_policy(
        payload_factory=_payload_factory,
        initial_max_tokens=max_tokens,
        min_response_tokens=min_response_tokens,
        headers=headers,
        url=cfg["completions_url"],
        verify=cfg.get("use_ssl", True),
        use_retry=cfg.get("use_retry", False),
        logger=getattr(ctx, "logger", None),
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
