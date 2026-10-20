"""User-stop tool-result envelopes A/B — не смешивать с cycle-stop."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

#: Причина для модели — одна на конверт A и тело отказа защиты.
CANCELLED_BY_USER_MESSAGE = "Tool was interrupted by user stop."


def cancelled_by_user_output(
    tool_name: str,
    *,
    meta: Optional[Dict[str, Any]] = None,
    message: str = CANCELLED_BY_USER_MESSAGE,
) -> str:
    """Envelope A: тул уже запущен, прерван user-stop."""
    payload: Dict[str, Any] = {
        "tool": str(tool_name or "unknown_tool"),
        "ok": False,
        "status": "cancelled_by_user",
        "message": message,
        "error": {
            "code": "cancelled_by_user",
            "message": message,
            "retryable": True,
        },
    }
    if meta is not None:
        payload["meta"] = dict(meta)
    return json.dumps(payload, ensure_ascii=False)


def cancelled_by_user_error(**fields: Any) -> dict:
    """Тело отказа защиты, прерванной user-stop до решения (стоп во время
    запроса к судье действий): действие не выполнялось. Форма — как у тел
    отказа режима (``type`` и поля защиты), смысл — как у конверта A."""
    return {
        "type": "cancelled_by_user",
        **fields,
        "reason": CANCELLED_BY_USER_MESSAGE,
        "retryable": True,
    }


def not_started_due_to_user_stop_output(tool_name: str) -> str:
    """Envelope B: call в batch был, apply_tool не вызывали из‑за stop."""
    payload = {
        "tool": str(tool_name or "unknown_tool"),
        "ok": False,
        "status": "not_started_due_to_user_stop",
        "message": "Tool was not started due to manual user stop.",
        "error": None,
        "synthetic": True,
    }
    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "cancelled_by_user_error",
    "cancelled_by_user_output",
    "not_started_due_to_user_stop_output",
]
