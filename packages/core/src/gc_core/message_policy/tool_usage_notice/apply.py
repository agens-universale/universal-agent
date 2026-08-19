from __future__ import annotations

from typing import Any

from gc_core.config_modules.message_notice import active_tool_usage_notice_rules
from gc_core.config_modules.project_memory import project_memory_from_config_dict
from gc_core.message_policy.project_memory_notice.policy import (
    collect_project_memory_hints,
    on_persist_steps,
    project_memory_hints_enabled,
    wrap_project_memory_hint_user_message,
)
from gc_core.constants.tool_names import PROJECT_MEMORY_TOOL_NAME
from gc_core.message_policy.notice_policy_log import log_notice_policy_step
from gc_core.message_policy.todo_reminder import apply_todo_reminder
from gc_core.message_policy.tool_availability import available_tool_names, tool_is_available
from gc_core.message_policy.tool_usage_notice.policy import (
    batch_used_tool,
    collect_tool_usage_notices,
    get_thread_memory_registers_from_shared,
    update_tool_usage_notice_after_persist,
    wrap_tool_usage_notice_user_message,
)
from gc_core.message_policy.tool_usage_notice.state import (
    get_project_memory_check_cnt,
    persist_tool_usage_notice_state,
)


def _tool_thresholds(rules: list[Any]) -> dict[str, int]:
    return {rule.tool_name: rule.step_messages_count for rule in rules}


def _tool_reset_reasons(
    step_messages: list[dict[str, Any]],
    rules: list[Any],
    *,
    counters_before: dict[str, int],
    counters_after: dict[str, int],
    emitted_tools: set[str],
) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for rule in rules:
        tool_name = rule.tool_name
        before = counters_before.get(tool_name, 0)
        after = counters_after.get(tool_name, 0)
        if before == after:
            continue
        if tool_name in emitted_tools:
            reasons[tool_name] = "notice emitted"
        elif batch_used_tool(step_messages, tool_name):
            reasons[tool_name] = "tool used in batch"
        elif after == 0:
            reasons[tool_name] = "counter cleared"
        else:
            reasons[tool_name] = "incremented"
    return reasons


def apply_tool_usage_notice_before_llm(
    messages: list[dict[str, Any]],
    state: Any,
    ctx: Any,
    meta: dict[str, Any],
    *,
    step_messages: list[dict[str, Any]],
    logger: Any | None = None,
) -> list[dict[str, Any]]:
    """
    После persist-batch, перед LLM: независимые контуры project_memory K-тик и tool_usage_notice.
    Счётчики не сбрасывают друг друга; снимок — только в текстовый log.
    """
    cfg = getattr(ctx, "config_dict", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    # Единый гейт канала: подсказываем только про тулы, которые в этом ране есть.
    # Проверки по конфигу ниже (stop_tools) остаются — этот гейт строго у́же и
    # ловит то, чего конфиг на этом уровне не знает: сужение набора дочернего рана
    # файлом сабагента, harbor, неподнявшийся MCP. См. tool_availability.
    available_tools = available_tool_names(ctx, state)

    notice_rules = [
        rule
        for rule in active_tool_usage_notice_rules(cfg)
        if tool_is_available(available_tools, rule.tool_name)
    ]
    tool_thresholds = _tool_thresholds(notice_rules)
    counters_before = dict(getattr(state, "tool_usage_notice_cnt", None) or {})

    project_hints_enabled = project_memory_hints_enabled(cfg) and tool_is_available(
        available_tools, PROJECT_MEMORY_TOOL_NAME
    )
    project_interval = (
        project_memory_from_config_dict(cfg).check_interval_messages
        if project_hints_enabled
        else None
    )
    project_check_after_tick: int | None = None

    if project_hints_enabled:
        project_check_after_tick = on_persist_steps(meta, steps=1)

    if notice_rules:
        update_tool_usage_notice_after_persist(state, step_messages, notice_rules)

    counters_before_collect = dict(getattr(state, "tool_usage_notice_cnt", None) or {})
    out = list(messages)

    emitted_project_hints: list[str] = []
    if project_hints_enabled:
        for hint_text in collect_project_memory_hints(state, meta, ctx, cfg):
            out.append(wrap_project_memory_hint_user_message(hint_text, suffix="mem_project"))
            emitted_project_hints.append(hint_text)

    project_check_final = get_project_memory_check_cnt(meta) if project_hints_enabled else None
    project_reset = (
        project_interval is not None
        and project_check_after_tick is not None
        and project_check_after_tick >= project_interval
        and project_check_final == 0
    )

    shared = getattr(state, "shared", None)
    registers = get_thread_memory_registers_from_shared(shared)

    for notice_text in collect_tool_usage_notices(state, notice_rules, registers=registers):
        out.append(wrap_tool_usage_notice_user_message(notice_text))

    out = apply_todo_reminder(
        out,
        state,
        cfg,
        step_messages=step_messages,
        available_tools=available_tools,
    )

    counters_final = dict(getattr(state, "tool_usage_notice_cnt", None) or {})
    emitted_tools = {
        name
        for name, threshold in tool_thresholds.items()
        if counters_before_collect.get(name, 0) >= threshold
    }
    tool_resets = _tool_reset_reasons(
        step_messages,
        notice_rules,
        counters_before=counters_before,
        counters_after=counters_final,
        emitted_tools=emitted_tools,
    )

    persist_tool_usage_notice_state(state, meta)

    log_notice_policy_step(
        logger,
        tool_counters=counters_final,
        tool_thresholds=tool_thresholds,
        tool_resets=tool_resets,
        tool_emits=sorted(emitted_tools),
        long_check_cnt=project_check_final if project_hints_enabled else None,
        long_interval=project_interval,
        long_reset=project_reset,
        long_emit=bool(emitted_project_hints),
    )
    return out
