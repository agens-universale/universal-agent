"""Отказы режима разрешений для модели: конверт отказа и инструкции после него.

Защиты (запись / чтение / команды / сеть), гейт диспатча и судья действий
отдают отказ одним конвертом: ``reason`` — человеку, ``guidance`` — инструкция
модели. Здесь — сами тексты и сборка конверта; решение «отказать» принимает
политика (``gc_core.security.permission_mode``).
"""

from __future__ import annotations

import json
from typing import Any

from gc_core.constants.tool_names import PROPOSE_PLAN_TOOL_NAME

#: Запрет повтора касается инициативы модели, а не просьбы человека: он мог
#: передумать или сменить режим, и узнать это можно только новым вызовом.
RETRY_ON_REQUEST = (
    "If the user later explicitly asks for this action again, make the call "
    "again: the answer may have changed."
)

#: Инструкция модели после отказа режима. Отдельным полем `guidance`, а не
#: приклейкой к причине — та же форма конверта, что у отказов модерации
#: команд: `reason` показывается человеку, `guidance` читает модель.
DENIED_GUIDANCE = (
    "Do NOT retry the same call on your own, and do NOT reach the same result "
    "another way (a different tool, a rewritten or split-up request) — report "
    "the block to the user and ask how to proceed. " + RETRY_ON_REQUEST
)

#: Отказ в режиме plan. Выход здесь не «спроси человека», а «предложи план»:
#: без этой подсказки агент, получив запрет на запись, советует сменить режим.
PLAN_DENIED_GUIDANCE = (
    "Plan mode is active: nothing may be changed until the user approves a "
    "plan. Do NOT retry the call and do NOT reach the same result another "
    f"way. Finish the research and propose the plan via {PROPOSE_PLAN_TOOL_NAME}."
)


#: То же для субагента: `turn_propose_plan` ему не предлагается и отвечает
#: отказом — план предлагает родитель, субагент возвращает выводы.
PLAN_DENIED_SUBAGENT_GUIDANCE = (
    "Plan mode is active: nothing may be changed. Do NOT retry the call and do "
    "NOT reach the same result another way. Finish the research and return "
    "your findings as text."
)


def mode_denied_guidance(mode: object) -> str:
    """Инструкция модели после запрета режима."""
    # Локально: пакет security при импорте тянет защиты, а они — этот модуль.
    from gc_core.security.call_scope import current_call
    from gc_core.security.permission_mode import Mode

    value = mode.value if isinstance(mode, Mode) else str(mode or "")
    if value != Mode.PLAN.value:
        return DENIED_GUIDANCE
    scope = current_call()
    state = getattr(scope, "state", None)
    if getattr(state, "parent_state_id", None) is not None:
        return PLAN_DENIED_SUBAGENT_GUIDANCE
    return PLAN_DENIED_GUIDANCE


SESSION_DENIED_TYPE = "denied_by_user_for_session"
#: Причина для человека: её показывает лента.
SESSION_DENIED_REASON = "Denied by the user for this session."
#: Запрет поставил сам человек — модель обязана ему об этом сказать, а не
#: молча искать другой путь.
SESSION_DENIED_GUIDANCE = (
    "Do NOT retry the same call on your own, and do NOT reach the same result "
    "another way — tell the user it is blocked by their earlier answer and ask "
    "the user how to proceed. " + RETRY_ON_REQUEST
)


def mode_denied_error(
    error_type: str,
    reason: str,
    *,
    guidance: str = DENIED_GUIDANCE,
    **fields: Any,
) -> dict:
    """Тело отказа: ``reason`` — человеку, ``guidance`` — инструкция модели.
    Одна сборка на гейт и защиты."""
    return {
        "type": error_type,
        **fields,
        "reason": reason,
        "guidance": guidance,
    }


DECLINED_TYPE = "declined_by_user"
#: Человек ответил «нет» на вопрос. Это его решение по одному действию, а не
#: запрет режима: лента и модель должны видеть именно это.
DECLINED_REASON = "Declined by the user."
DECLINED_GUIDANCE = (
    "The user answered no to this action. Do NOT retry the same call on your "
    "own, and do NOT reach the same result another way — continue without it, "
    "or stop and report that it was declined. " + RETRY_ON_REQUEST
)

UNATTENDED_TYPE = "confirmation_unavailable"
#: Действие требует подтверждения, а спросить некого (запуск без человека).
UNATTENDED_REASON = (
    "Needs the user's confirmation, and there is no one to ask in this run."
)


def declined_error(**fields: Any) -> dict:
    """Тело отказа: человек ответил «нет» на вопрос подтверждения."""
    return mode_denied_error(
        DECLINED_TYPE, DECLINED_REASON, guidance=DECLINED_GUIDANCE, **fields
    )


def unattended_error(**fields: Any) -> dict:
    """Тело отказа: подтверждение нужно, но человека в запуске нет."""
    return mode_denied_error(UNATTENDED_TYPE, UNATTENDED_REASON, **fields)


def session_denied_error(**fields: Any) -> dict:
    """Тело отказа по запомненному запрету человека."""
    return mode_denied_error(
        SESSION_DENIED_TYPE,
        SESSION_DENIED_REASON,
        guidance=SESSION_DENIED_GUIDANCE,
        **fields,
    )


ACTION_BLOCKED_TYPE = "action_blocked"
#: Отказ судьи действий. В отличие от отказа режима, более безопасный путь к
#: той же цели не запрещён — запрещён обход тем же действием.
ACTION_BLOCKED_GUIDANCE = (
    "The automatic review blocked this action. Do NOT retry the same call and "
    "do NOT reach the same result another way. Continue with a materially "
    "safer alternative or with the rest of the task, and report to the user "
    "what was blocked and why. " + RETRY_ON_REQUEST
)
#: Добавка к отказам на паузе, когда спросить некого: попыток у агента больше нет.
ACTION_BLOCKED_REPEATED = (
    " Several actions in a row were blocked: Stop trying variations of this "
    "action and report to the user."
)


#: Причина отказа на паузе без человека — для человека, как слова судьи.
ACTION_PAUSED_REASON = (
    "Проверка действий в режиме auto приостановлена после повторных отказов; "
    "нужно решение человека."
)


def judge_failures_stop_text(failures: int) -> str:
    """Причина остановки прогона — для человека: проверки действий подряд не
    состоялись."""
    return (
        f"Автоматическая проверка действий недоступна: {failures} проверок "
        "подряд не состоялись. Прогон остановлен — повторите позже или "
        "смените режим разрешений."
    )


#: Причина остановки прогона — для человека: у конфигурации нет канала
#: вспомогательной модели, судье действий не на чем работать.
JUDGE_NO_CHANNEL_STOP_TEXT = (
    "Режим разрешений auto недоступен в этой конфигурации: нет канала "
    "вспомогательной модели для проверки действий. Прогон остановлен — "
    "смените режим разрешений."
)


def action_blocked_error(
    reason: str, *, repeated: bool = False, **fields: Any
) -> dict:
    """Тело отказа судьи действий; `reason` — его слова, для человека."""
    guidance = ACTION_BLOCKED_GUIDANCE + (
        ACTION_BLOCKED_REPEATED if repeated else ""
    )
    return mode_denied_error(
        ACTION_BLOCKED_TYPE,
        reason or "The action is risky and the user did not authorize it.",
        guidance=guidance,
        **fields,
    )


def format_tool_denied(tool_name: str, error: dict) -> str:
    """Отказ в виде обычного tool-output — тот же конверт, что у защит."""
    return json.dumps(
        {"tool": tool_name, "ok": False, "error": error}, ensure_ascii=False
    )
