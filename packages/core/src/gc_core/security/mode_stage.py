"""Исполнение решения режима разрешений — общее для всех защит.

Каждая защита сама понимает, что за действие перед ней (путь внутри проекта
или вне, безобидная ли команда, прошёл ли адрес политику), и сама спрашивает
у режима исход. Здесь — то, что у всех одинаково: запомненный запрет
человека, судья действий, вопрос человеку и отказ режима, с записью в аудит.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from gc_core.message_policy.permission_denials import (
    mode_denied_error,
    mode_denied_guidance,
    session_denied_error,
)
from gc_core.security.confirm_port import (
    confirmation_step,
    unconfirmed_error,
)
from gc_core.security.permission_mode import (
    ALIGN,
    ASK,
    resolve_mode,
    session_deny,
)


@dataclass(frozen=True)
class ModeAction:
    """Действие, о котором решает режим, — как его видит защита.

    ``error_fields`` — поля конверта отказа (путь, команда, адрес);
    ``deny_fields`` — поля отказа самого режима, если они другие.
    """

    context: Any
    tool_name: str
    category: str
    decision_key: str
    question: tuple[str, str, list]
    error_fields: Mapping[str, Any]
    deny_type: str
    deny_reason: str
    deny_fields: Mapping[str, Any] | None = None
    flags: tuple[str, ...] = ()
    runtime: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None


def audit(
    context: Any, tool_name: str, outcome: str, reason_code: str
) -> None:
    # Локально: защита записи, где живёт аудит, сама зовёт этот модуль.
    from gc_core.security.path_policy import audit_policy_decision

    audit_policy_decision(
        context,
        tool_name=tool_name,
        decision=outcome,
        reason_code=reason_code,
    )


def remembered_denial(
    context: Any, tool_name: str, decision_key: str, **fields: Any
) -> dict | None:
    """Запомненный человеком запрет «в сессии» — тело отказа, иначе None.

    Сильнее режима: проверяется раньше его разрешений и в любом режиме."""
    if not session_deny(context.ctx, decision_key):
        return None
    audit(context, tool_name, "blocked", "session_rule_deny")
    return session_denied_error(**fields)


def resolve_by_mode(decision: str, action: ModeAction):
    """Генератор для ``yield from``: ``(разрешено, тело отказа | None)``.

    ``decision`` — исход режима, который защита не решила сама: судья
    (ALIGN), вопрос (ASK) или отказ (всё прочее)."""
    if decision == ALIGN:
        from gc_core.security.action_judge_step import judged_step

        return (
            yield from judged_step(
                context=action.context,
                tool_name=action.tool_name,
                category=action.category,
                decision_key=action.decision_key,
                question=action.question,
                flags=action.flags,
                runtime=action.runtime,
                error_fields=action.error_fields,
            )
        )
    ctx = action.context.ctx
    if decision == ASK:
        title, question, fields = action.question
        confirmed = yield confirmation_step(
            title=title,
            question=question,
            fields=fields,
            ctx=ctx,
            decision_key=action.decision_key,
        )
        if confirmed:
            audit(
                action.context,
                action.tool_name,
                "executed",
                "mode_ask_confirmed",
            )
            return True, None
        audit(
            action.context, action.tool_name, "blocked", "mode_ask_cancelled"
        )
        return False, unconfirmed_error(ctx, **action.error_fields)
    audit(action.context, action.tool_name, "blocked", "mode_deny")
    deny_fields = (
        action.error_fields
        if action.deny_fields is None
        else action.deny_fields
    )
    return False, mode_denied_error(
        action.deny_type,
        action.deny_reason,
        guidance=mode_denied_guidance(resolve_mode(ctx)),
        **deny_fields,
    )
