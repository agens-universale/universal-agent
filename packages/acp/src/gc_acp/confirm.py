"""Confirm гварда → ACP ``session/request_permission``.

Порт блокирует поток тула до ответа клиента. Четыре ``optionId`` мапятся
в choice ядра; политика ``permission_store`` остаётся в ``apply_confirm_choice``.
"""

from __future__ import annotations

from typing import Any

from gc_core.execution.human import HumanRequest
from gc_core.security.confirm_port import (
    CONFIRM_ALLOW_ONCE,
    CONFIRM_ALLOW_REMEMBER,
    CONFIRM_DENY_ONCE,
    CONFIRM_DENY_REMEMBER,
)

from gc_acp.protocol import PARSE_ERROR, Incoming

_METHOD = "session/request_permission"
_OPTIONS = (
    {
        "optionId": "allow_once",
        "name": "Allow once",
        "kind": "allow_once",
    },
    {
        "optionId": "allow_always",
        "name": "Allow always",
        "kind": "allow_always",
    },
    {
        "optionId": "reject_once",
        "name": "Reject once",
        "kind": "reject_once",
    },
    {
        "optionId": "reject_always",
        "name": "Reject always",
        "kind": "reject_always",
    },
)
_OPTION_TO_CHOICE = {
    "allow_once": CONFIRM_ALLOW_ONCE,
    "allow_always": CONFIRM_ALLOW_REMEMBER,
    "reject_once": CONFIRM_DENY_ONCE,
    "reject_always": CONFIRM_DENY_REMEMBER,
}
_KIND = {"read": "read", "write": "edit", "command": "execute"}
_DEFAULT_TIMEOUT_SEC = 600.0


def bind_confirm(agent: Any, session_id: str, facade: Any) -> None:
    """Поставить ``ctx.confirm_request`` на сессию ACP.

    ``bind_ask_human`` не заменяет: свободный вопрос человека в ACP
    по-прежнему пустой.
    """

    def confirm_request(request: HumanRequest) -> str:
        return facade.confirm_blocking(session_id, request)

    agent.executor._ctx.confirm_request = confirm_request


def timeout_sec(settings: Any) -> float:
    inner = getattr(settings, "settings", None)
    host = inner if inner is not None else settings
    acp = getattr(host, "acp", None)
    raw = getattr(acp, "confirm_timeout_sec", _DEFAULT_TIMEOUT_SEC)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SEC
    if value < 0:
        return _DEFAULT_TIMEOUT_SEC
    return value


def permission_message(
    request_id: Any,
    session_id: str,
    request: HumanRequest,
    open_ids: list[str],
) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": _METHOD,
        "params": {
            "sessionId": session_id,
            "toolCall": {
                "toolCallId": _tool_call_id(request.decision_key, open_ids),
                "title": request.title,
                "kind": _tool_kind(request.decision_key),
                "status": "pending",
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": _body(request)},
                    }
                ],
            },
            "options": [dict(item) for item in _OPTIONS],
        },
    }


def choice_from_incoming(incoming: Incoming) -> str:
    """Ответ клиента → choice. Всё неясное — ``deny_once``."""
    if incoming.has_error or incoming.parse_error:
        return CONFIRM_DENY_ONCE
    result = incoming.result
    if not isinstance(result, dict):
        return CONFIRM_DENY_ONCE
    outcome = result.get("outcome")
    if not isinstance(outcome, dict):
        return CONFIRM_DENY_ONCE
    kind = outcome.get("outcome")
    if kind == "cancelled":
        return CONFIRM_DENY_ONCE
    if kind != "selected":
        return CONFIRM_DENY_ONCE
    mapped = _OPTION_TO_CHOICE.get(outcome.get("optionId"))
    if mapped is None:
        return CONFIRM_DENY_ONCE
    return mapped


def cancelled_reply() -> Incoming:
    return Incoming(
        has_result=True,
        result={"outcome": {"outcome": "cancelled"}},
    )


def broken_reply() -> Incoming:
    return Incoming(
        has_error=True,
        error={"code": PARSE_ERROR, "message": "Parse error"},
    )


def _tool_kind(decision_key: str | None) -> str:
    if not isinstance(decision_key, str) or ":" not in decision_key:
        return "other"
    prefix = decision_key.split(":", 1)[0]
    return _KIND.get(prefix, "other")


def _tool_call_id(decision_key: str | None, open_ids: list[str]) -> str:
    if open_ids:
        return open_ids[-1]
    key = decision_key if isinstance(decision_key, str) else ""
    return f"confirm:{key}"


def _body(request: HumanRequest) -> str:
    lines = [request.question]
    for row in request.fields:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            lines.append(f"{row[0]}: {row[1]}")
        else:
            lines.append(str(row))
    return "\n".join(lines)
