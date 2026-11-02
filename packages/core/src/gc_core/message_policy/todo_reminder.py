"""Todo staleness reminder: technical notice, когда чек-лист session_todo протух.

Контур независим от tool_usage_notice-правил: свой счётчик
``state.todo_reminder_cnt`` (переживает границу хода через thread meta).
Две ветки: есть незакрытые пункты — директивное «обнови»; живого плана нет
(пусто ИЛИ всё закрыто) — мягкое «если задача того стоит, заведи», один раз.
Вызывается из ``notice_texts_before_llm`` (chat_completions-only).
"""

from __future__ import annotations

from typing import Any

from gc_core.constants.tool_names import SESSION_TODO_TOOL_NAME
from gc_core.message_policy.todo_context import format_todo_snapshot
from gc_core.message_policy.tool_availability import tool_is_available
from gc_core.message_policy.tool_usage_notice.policy import batch_used_tool
from gc_core.settings import AppConfig, settings_of

_OPEN_STATUSES = ("pending", "in_progress")


def _todo_items(state: Any) -> list[dict]:
    raw = getattr(state, "session_todo", None)
    return raw if isinstance(raw, list) else []


def has_open_todos(items: list) -> bool:
    return any(
        isinstance(item, dict) and item.get("status") in _OPEN_STATUSES
        for item in items
    )


def build_todo_create_nudge_text() -> str:
    """Мягкая подсказка завести чек-лист: живого плана нет, а работа идёт долго.

    Условная и отклоняемая формулировка намеренно: заводить список или нет —
    решение модели по тому же критерию, что в описании тула; харнес лишь
    напоминает о такой возможности, когда работа затянулась."""
    return (
        "You have been working for a while without an active task checklist "
        "(session_todo). If this task would benefit from tracking progress — "
        "several logical steps, multiple deliverables, or a list of requirements "
        "to satisfy — consider writing the checklist now and keeping it current "
        "as you go. Skip this if the task is simple or nearly done."
    )


def build_todo_reminder_text(items: list) -> str:
    return (
        "Your task checklist (session_todo) has not been updated for a while. "
        "Current state:\n"
        f"{format_todo_snapshot(items)}\n"
        "Review it now with the session_todo tool: mark finished steps completed, "
        "set the current step to in_progress, or replace/clear the list if it no "
        "longer matches the task."
    )


def _stopped(settings: AppConfig) -> bool:
    return SESSION_TODO_TOOL_NAME in {
        str(item).strip()
        for item in settings.agent.stop_tools
        if isinstance(item, str)
    }


def todo_reminder_text(
    state: Any,
    settings: Any,
    *,
    step_messages: list[dict[str, Any]],
    available_tools: set[str] | None = None,
    invoked_tool_names: set[str] | None = None,
) -> str | None:
    """Текст напоминания либо None. Счётчик тикает здесь, сообщение не собирает."""
    settings = settings_of(settings)
    steps_limit = max(0, int(settings.todo.reminder_steps))
    if steps_limit <= 0 or _stopped(settings):
        return None
    if not tool_is_available(available_tools, SESSION_TODO_TOOL_NAME):
        return None

    items = _todo_items(state)
    invoked = invoked_tool_names or set()
    if SESSION_TODO_TOOL_NAME in invoked or batch_used_tool(
        step_messages, SESSION_TODO_TOOL_NAME
    ):
        state.todo_reminder_cnt = 0
        return None

    state.todo_reminder_cnt = (
        int(getattr(state, "todo_reminder_cnt", 0) or 0) + 1
    )
    if state.todo_reminder_cnt < steps_limit:
        return None

    # Порог достигнут — отсчёт начинается заново НЕЗАВИСИМО от того, отправим ли
    # мы сообщение. Иначе в подавленной ветке (подсказка уже была) счётчик растёт
    # до конца хода, персистится раздутым, а флаг подсказки — per-run: следующий
    # ход получал бы напоминание на первом же шаге вместо N-го.
    state.todo_reminder_cnt = 0

    # Живого плана нет (списка нет ИЛИ он весь закрыт) — мягкая подсказка завести,
    # один раз за прогон: отклонённое решение модели не оспариваем повтором.
    if not has_open_todos(items):
        if getattr(state, "todo_create_nudge_sent", False):
            return None
        state.todo_create_nudge_sent = True
        return build_todo_create_nudge_text()
    return build_todo_reminder_text(items)
