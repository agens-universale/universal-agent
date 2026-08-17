"""User-stop tool-result envelopes A/B — не смешивать с cycle-stop."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


def cancelled_by_user_output(
    tool_name: str,
    *,
    meta: Optional[Dict[str, Any]] = None,
    message: str = "Tool was interrupted by user stop.",
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
    "cancelled_by_user_output",
    "not_started_due_to_user_stop_output",
]
