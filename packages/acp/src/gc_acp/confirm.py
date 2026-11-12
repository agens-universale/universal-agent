"""Confirm гварда → ACP ``session/request_permission``.

Порт блокирует поток тула до ответа клиента. Четыре ``optionId`` мапятся
в choice ядра; политика ``permission_store`` остаётся в ``apply_confirm_choice``.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from gc_core.execution.human import HumanRequest
from gc_core.security.confirm_port import (
    CONFIRM_ALLOW_ONCE,
    CONFIRM_ALLOW_REMEMBER,
    CONFIRM_DENY_ONCE,
    CONFIRM_DENY_REMEMBER,
    CONFIRM_TIMED_OUT,
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
# Quick pick VS Code рисует title и молчит про content. Команда должна
# помещаться в заголовок, длинный payload режется.
_TITLE_COMMAND_LIMIT = 200
# Конец длинной команды в заголовке остаётся виден: туда дописывают
# «; curl … | sh» за безобидным началом.
_TITLE_TAIL = 60
_ELLIPSIS = " … "
# Обрезанную команду человек целиком не видел: «навсегда» её не одобрить
# (решение запомнилось бы и ушло судье примером разрешённой команды).
_ALLOW_ALWAYS = "allow_always"


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
    tool_call_id: str | None,
) -> dict:
    title, clipped = _permission_title(request)
    options = [
        dict(item)
        for item in _OPTIONS
        if not (clipped and item["optionId"] == _ALLOW_ALWAYS)
    ]
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": _METHOD,
        "params": {
            "sessionId": session_id,
            "toolCall": {
                "toolCallId": tool_call_id
                or _synthetic_id(request.decision_key),
                "title": title,
                "kind": _tool_kind(request.decision_key),
                "status": "pending",
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": _body(request)},
                    }
                ],
            },
            "options": options,
        },
    }


def choice_from_incoming(incoming: Incoming) -> str:
    """Ответ клиента → choice.

    Явные четыре optionId пишутся в стор. Отмена, ошибка JSON-RPC и
    неизвестный optionId — отказ без записи, как голый таймаут: человек
    reject_once не выбирал.
    """
    if incoming.has_error or incoming.parse_error:
        return CONFIRM_TIMED_OUT
    result = incoming.result
    if not isinstance(result, dict):
        return CONFIRM_TIMED_OUT
    outcome = result.get("outcome")
    if not isinstance(outcome, dict):
        return CONFIRM_TIMED_OUT
    if outcome.get("outcome") != "selected":
        return CONFIRM_TIMED_OUT
    mapped = _OPTION_TO_CHOICE.get(outcome.get("optionId"))
    if mapped is None:
        return CONFIRM_TIMED_OUT
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


def _synthetic_id(decision_key: str | None) -> str:
    key = decision_key if isinstance(decision_key, str) else ""
    return f"confirm:{key}"


def _permission_title(request: HumanRequest) -> tuple[str, bool]:
    """Заголовок диалога и признак «команда в нём обрезана».

    Для команды заголовок несёт и сам payload: клиент, который не рисует
    карточку и не читает ``content``, показывает только эту строку. Длинная
    команда — начало и конец с пометкой ``⚠ … [обрезано, N симв.]``: без
    конца в заголовке не видно приписанного за безобидным началом.
    """
    base = request.title if isinstance(request.title, str) else ""
    if _tool_kind(request.decision_key) != "execute":
        return base, False
    payload = _command_payload(request)
    if payload is None:
        return base, False
    flat = _one_line(payload)
    if flat == "":
        return base, False
    if len(flat) <= _TITLE_COMMAND_LIMIT:
        return (f"{base}: {flat}" if base else flat), False
    head = _TITLE_COMMAND_LIMIT - _TITLE_TAIL - len(_ELLIPSIS)
    shown = f"{flat[:head]}{_ELLIPSIS}{flat[-_TITLE_TAIL:]}"
    marker = f"[обрезано, {len(flat)} симв.]"
    label = f"⚠ {base} {marker}" if base else f"⚠ {marker}"
    return f"{label}: {shown}", True


def _command_payload(request: HumanRequest) -> str | None:
    """Первое поле гварда команды: «Команда», «Код» или ``command``."""
    for row in request.fields:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            value = row[1]
            if isinstance(value, str):
                return value
            if value is not None:
                return str(value)
        return None
    return None


def _one_line(text: str) -> str:
    """Одна строка без невидимого: пробелы и переводы строк — один пробел,
    управляющие и форматирующие символы (U+202E разворачивает текст, нулевой
    ширины прячет) — видимой escape-записью."""
    return " ".join(_visible(text).split())


def _visible(text: str) -> str:
    return "".join(
        ch
        if ch in "\n\r\t" or unicodedata.category(ch) not in ("Cc", "Cf")
        else f"\\u{ord(ch):04x}"
        for ch in text
    )


def _body(request: HumanRequest) -> str:
    lines = [_visible(str(request.question))]
    for row in request.fields:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            lines.append(f"{_visible(str(row[0]))}: {_visible(str(row[1]))}")
        else:
            lines.append(_visible(str(row)))
    return "\n".join(lines)
