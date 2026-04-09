"""Shared chat/completions client helpers for controller-level modules."""

from typing import Any, Dict, List

from utils.connect_backend import create_call_headers, request_backend_chat_completions


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


def request_chat_completion_text(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    max_tokens: int,
    request_label: str = "chat_completion",
) -> str:
    cfg = getattr(ctx, "config_dict", {}) or {}
    payload = {
        "model": getattr(state, "default_model", None) or cfg.get("model"),
        "messages": messages,
        "tools": [],
        "temperature": 0.01,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "max_tokens": max_tokens,
        "repetition_penalty": 1.1,
        "separate_reasoning": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    _ = request_label  # for call-site traceability and future extension
    headers = create_call_headers(state, ctx)
    res = request_backend_chat_completions(
        payload,
        headers,
        url=cfg["completions_url"],
        verify=cfg.get("use_ssl", True),
        use_retry=cfg.get("use_retry", False),
        logger=getattr(ctx, "logger", None),
    )
    text = (
        res.get("choices", [{}])[0].get("message", {}).get("content")
        if isinstance(res, dict)
        else ""
    )
    return (text or "").strip()
