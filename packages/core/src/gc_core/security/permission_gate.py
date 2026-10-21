"""Гейт режима разрешений перед вызовом тула.

Решает категории БЕЗ собственной защиты: MCP, запуск скрипта скилла, тул без
разметки. Запись, чтение, команды и сеть решают их защиты — им виден путь,
команда или адрес.

Тело гейта — генератор той же формы, что тела защит (см. ``dual_wrapper``):
``return (yield)`` — «зови тул», вопрос человеку — ``yield confirmation_step``.
Исполняет его тот же драйвер, поэтому ожидание человека здесь устроено ровно
так же, как у защит, и второй реализации не заводится.
"""

from __future__ import annotations

from typing import Any

from gc_core.message_policy.permission_denials import (
    format_tool_denied,
)
from gc_core.security.mode_stage import (
    ModeAction,
    remembered_denial,
    resolve_by_mode,
)
from gc_core.security.path_policy import (
    resolve_tool_context,
)
from gc_core.security.permission_mode import (
    ALLOW,
    CAT_MCP,
    PASS,
    call_category,
    category_decision,
    decision_identity,
    gate_decision_key,
    resolve_mode,
)


def _question(tool_name: str, category: str, entry: dict, arguments: Any):
    """Заголовок, вопрос и поля подтверждения для действия гейта."""
    if category == CAT_MCP:
        server = str(entry.get("mcp_server") or "").strip()
        return (
            "MCP-инструмент",
            f"Разрешить вызов «{tool_name}»?",
            [("Сервер", server or "—"), ("Инструмент", tool_name)],
        )
    identity = decision_identity(entry, arguments)
    what = f"«{tool_name}»"
    if identity:
        what += f" ({' / '.join(value for _, value in identity)})"
    return (
        "Разрешение на действие",
        f"Разрешить вызов {what}?",
        [("Инструмент", tool_name), ("Категория", category), *identity],
    )


def permission_gate_flow(
    tool_name: str,
    entry: dict | None,
    ctx: Any,
    arguments: Any = None,
    *,
    state: Any = None,
):
    """Поток гейта: отказ — готовая строка tool-output, иначе результат тула."""
    entry = entry or {}
    category = call_category(entry, arguments)
    decision = category_decision(category, ctx)
    if decision == PASS:
        return (yield)

    key = gate_decision_key(tool_name, entry, arguments)
    context = resolve_tool_context(ctx=ctx, state=state)
    denied = remembered_denial(context, tool_name, key, category=category)
    if denied is not None:
        return format_tool_denied(tool_name, denied)
    if decision == ALLOW:
        return (yield)
    mode = resolve_mode(ctx).value
    allowed, error = yield from resolve_by_mode(
        decision,
        ModeAction(
            context=context,
            tool_name=tool_name,
            category=category,
            decision_key=key,
            question=_question(tool_name, category, entry, arguments),
            error_fields={"category": category},
            deny_type="permission_denied_by_mode",
            deny_reason=(
                f"Permission mode '{mode}' does not allow '{category}' actions."
            ),
            deny_fields={"mode": mode, "category": category},
        ),
    )
    if allowed:
        return (yield)
    return format_tool_denied(tool_name, error)
