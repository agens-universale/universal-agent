"""Одобрение плана в ACP: свой ``session/request_permission`` после хода.

Не порт подтверждения гварда (``gc_acp.confirm``): у того четыре зашитых
allow/reject, он синхронный и привязан к карточке идущего тула, а план
предлагается, когда ход уже кончился. Здесь — варианты режимов исполнения
и доработки (как у баннера TUI), свой разбор ответа и явная карточка плана.
Сама последовательность одобрения — ядра (``prepare_accepted_plan``):
прогон продолжения ACP ведёт своим потоком дельт.
"""

from __future__ import annotations

import uuid
from typing import Any

from gc_core.plan_store import PLAN_STATUS_PROPOSED
from gc_core.security.permission_mode import (
    PLAN_EXECUTION_MODES,
    mode_label,
)

from gc_acp.protocol import Incoming

PLAN_REWORK = "rework"
#: Строка ленты после «доработать»: замечания — следующим сообщением.
REWORK_HINT = "Напишите следующим сообщением, что доработать в плане."
_METHOD = "session/request_permission"
_CARD_PREFIX = "plan_approval:"
_TITLE = "Агент составил план и готов приступить к реализации. Выполнять?"
_BODY = "План — в ответе выше. Выберите режим исполнения или доработку."
_OPTIONS = (
    *(
        {
            "optionId": mode.value,
            "name": f"Да, в {mode_label(mode)}",
            "kind": "allow_once",
        }
        for mode in PLAN_EXECUTION_MODES
    ),
    {
        "optionId": PLAN_REWORK,
        "name": "Обсудить и доработать",
        "kind": "reject_once",
    },
)
_CHOICES = frozenset(item["optionId"] for item in _OPTIONS)


def plan_awaits_approval(agent: Any, session_id: str) -> bool:
    """Спрашивать есть о чём: ИМЕННО этот прогон кончился планом, и план
    ждёт решения. Повторный показ утверждённого плана решения не просит."""
    executor = getattr(agent, "executor", None)
    if getattr(executor, "last_terminal_outcome", None) != "plan_proposed":
        return False
    plan = executor.plan_snapshot(session_id)
    return bool(plan) and plan.get("status") == PLAN_STATUS_PROPOSED


def plan_card_id() -> str:
    """Своя карточка на каждый вопрос: после хода текущей карточки нет."""
    return _CARD_PREFIX + uuid.uuid4().hex


def plan_permission_message(
    request_id: Any, session_id: str, card_id: str
) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": _METHOD,
        "params": {
            "sessionId": session_id,
            "toolCall": {
                "toolCallId": card_id,
                "title": _TITLE,
                "kind": "switch_mode",
                "status": "pending",
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": _BODY},
                    }
                ],
            },
            "options": [dict(item) for item in _OPTIONS],
        },
    }


def plan_card_closed(card_id: str, *, decided: bool) -> dict:
    """Карточка вопроса о плане закрыта: решение есть — ``completed``,
    нет (отмена, ошибка, таймаут, чужой ответ) — ``failed``."""
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": card_id,
        "status": "completed" if decided else "failed",
    }


def plan_choice_from_incoming(incoming: Incoming) -> str | None:
    """Ответ клиента → режим исполнения, ``PLAN_REWORK`` или None.

    None — решения нет (отмена, ошибка, чужой optionId): план остаётся
    предложенным, ход кончается без исполнения.
    """
    if incoming.has_error or incoming.parse_error:
        return None
    result = incoming.result
    outcome = result.get("outcome") if isinstance(result, dict) else None
    if not isinstance(outcome, dict):
        return None
    if outcome.get("outcome") != "selected":
        return None
    choice = outcome.get("optionId")
    return choice if choice in _CHOICES else None
