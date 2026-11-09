from __future__ import annotations

from typing import Any

from gc_core.constants.tool_names import PROJECT_MEMORY_TOOL_NAME
from gc_core.message_policy.notice_policy_log import log_notice_policy_step
from gc_core.message_policy.project_memory_notice.policy import (
    collect_project_memory_hints,
    on_persist_steps,
    project_memory_hints_enabled,
)
from gc_core.message_policy.todo_reminder import todo_reminder_text
from gc_core.message_policy.tool_availability import (
    available_tool_names,
    tool_is_available,
)
from gc_core.message_policy.tool_usage_notice.policy import (
    active_tool_usage_notice_rules,
    batch_used_tool,
    collect_tool_usage_notices,
    get_thread_memory_registers_from_shared,
    update_tool_usage_notice_after_persist,
)
from gc_core.message_policy.tool_usage_notice.state import (
    get_project_memory_check_cnt,
    persist_tool_usage_notice_state,
)
from gc_core.settings import AppConfig, settings_of


def _tool_thresholds(rules: list[Any]) -> dict[str, int]:
    return {rule.tool_name: rule.step_messages_count for rule in rules}


def _tool_reset_reasons(
    step_messages: list[dict[str, Any]],
    rules: list[Any],
    *,
    counters_before: dict[str, int],
    counters_after: dict[str, int],
    emitted_tools: set[str],
    invoked_tool_names: set[str] | None = None,
) -> dict[str, str]:
    invoked = invoked_tool_names or set()
    reasons: dict[str, str] = {}
    for rule in rules:
        tool_name = rule.tool_name
        before = counters_before.get(tool_name, 0)
        after = counters_after.get(tool_name, 0)
        if before == after:
            continue
        if tool_name in emitted_tools:
            reasons[tool_name] = "notice emitted"
        elif tool_name in invoked or batch_used_tool(step_messages, tool_name):
            reasons[tool_name] = "tool used in batch"
        elif after == 0:
            reasons[tool_name] = "counter cleared"
        else:
            reasons[tool_name] = "incremented"
    return reasons


def _emit_project_memory_hints(
    state: Any,
    meta: dict,
    ctx: Any,
    settings: AppConfig,
    *,
    enabled: bool,
    memory_hash: str | None,
) -> list[str]:
    """Тексты подсказок project_memory. Сообщение собирает вызывающий."""
    if not enabled:
        return []
    return collect_project_memory_hints(
        state, meta, ctx, settings, get_hash_fn=lambda _ctx: memory_hash
    )


def _project_counter_reset(
    interval: int | None, after_tick: int | None, final: int | None
) -> bool:
    return (
        interval is not None
        and after_tick is not None
        and after_tick >= interval
        and final == 0
    )


def _emitted_tools(
    tool_thresholds: dict[str, int], counters: dict[str, int]
) -> set[str]:
    return {
        name
        for name, threshold in tool_thresholds.items()
        if counters.get(name, 0) >= threshold
    }


def notice_texts_before_llm(
    state: Any,
    ctx: Any,
    meta: dict[str, Any],
    *,
    step_messages: list[dict[str, Any]],
    logger: Any | None = None,
    project_memory_hash: str | None = None,
    reset_tool_names: set[str] | None = None,
) -> list[str]:
    """Тексты подсказок в порядке: project memory, tool usage, todo.

    Сообщение ``user`` не собирает: его клеит цикл, в output или одним user.
    ``reset_tool_names`` — тулы, исполненные в батче; имя берётся из вызова.
    """
    return [
        text
        for _kind, text in _notice_pairs_before_llm(
            state,
            ctx,
            meta,
            step_messages=step_messages,
            logger=logger,
            project_memory_hash=project_memory_hash,
            reset_tool_names=reset_tool_names,
        )
    ]


def _notice_pairs_before_llm(
    state: Any,
    ctx: Any,
    meta: dict[str, Any],
    *,
    step_messages: list[dict[str, Any]],
    logger: Any | None = None,
    project_memory_hash: str | None = None,
    reset_tool_names: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Пары (вид, текст). Вид: memory, usage, todo. Порядок — канон списка."""
    settings = settings_of(ctx)
    if not isinstance(meta, dict):
        meta = {}

    # Единый гейт канала: подсказываем только про тулы, которые в этом ране есть.
    # Проверки по конфигу ниже (stop_tools) остаются — этот гейт строго у́же и
    # ловит то, чего конфиг на этом уровне не знает: сужение набора дочернего рана
    # файлом сабагента, harbor, неподнявшийся MCP. См. tool_availability.
    available_tools = available_tool_names(ctx, state)

    notice_rules = [
        rule
        for rule in active_tool_usage_notice_rules(settings)
        if tool_is_available(available_tools, rule.tool_name)
    ]
    tool_thresholds = _tool_thresholds(notice_rules)
    counters_before = dict(getattr(state, "tool_usage_notice_cnt", None) or {})

    project_hints_enabled = project_memory_hints_enabled(
        settings
    ) and tool_is_available(available_tools, PROJECT_MEMORY_TOOL_NAME)
    project_interval = (
        settings.project_memory.check_interval_messages
        if project_hints_enabled
        else None
    )
    project_check_after_tick: int | None = None

    if project_hints_enabled:
        project_check_after_tick = on_persist_steps(meta, steps=1)

    invoked = set(reset_tool_names or ())
    if notice_rules:
        update_tool_usage_notice_after_persist(
            state,
            step_messages,
            notice_rules,
            reset_tool_names=invoked,
        )

    counters_before_collect = dict(
        getattr(state, "tool_usage_notice_cnt", None) or {}
    )
    pairs: list[tuple[str, str]] = []

    emitted_project_hints = _emit_project_memory_hints(
        state,
        meta,
        ctx,
        settings,
        enabled=project_hints_enabled,
        memory_hash=project_memory_hash,
    )
    pairs.extend(("memory", text) for text in emitted_project_hints)
    project_check_final = (
        get_project_memory_check_cnt(meta) if project_hints_enabled else None
    )
    project_reset = _project_counter_reset(
        project_interval, project_check_after_tick, project_check_final
    )

    shared = getattr(state, "shared", None)
    registers = get_thread_memory_registers_from_shared(shared)

    for notice_text in collect_tool_usage_notices(
        state, notice_rules, registers=registers
    ):
        pairs.append(("usage", notice_text))

    todo_text = todo_reminder_text(
        state,
        settings,
        step_messages=step_messages,
        available_tools=available_tools,
        invoked_tool_names=invoked,
    )
    if todo_text:
        pairs.append(("todo", todo_text))

    counters_final = dict(getattr(state, "tool_usage_notice_cnt", None) or {})
    emitted_tools = _emitted_tools(tool_thresholds, counters_before_collect)
    tool_resets = _tool_reset_reasons(
        step_messages,
        notice_rules,
        counters_before=counters_before,
        counters_after=counters_final,
        emitted_tools=emitted_tools,
        invoked_tool_names=invoked,
    )

    persist_tool_usage_notice_state(state, meta)

    log_notice_policy_step(
        logger,
        tool_counters=counters_final,
        tool_thresholds=tool_thresholds,
        tool_resets=tool_resets,
        tool_emits=sorted(emitted_tools),
        long_check_cnt=project_check_final,
        long_interval=project_interval,
        long_reset=project_reset,
        long_emit=bool(emitted_project_hints),
    )
    return pairs
