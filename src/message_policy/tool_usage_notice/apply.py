from __future__ import annotations

from typing import Any

from config_modules.message_notice import active_tool_usage_notice_rules
from message_policy.long_memory_notice.policy import (
    collect_long_memory_hints,
    long_memory_hints_enabled,
    on_persist_steps,
    wrap_long_memory_hint_user_message,
)
from message_policy.tool_usage_notice.policy import (
    collect_tool_usage_notices,
    get_thread_memory_registers_from_shared,
    update_tool_usage_notice_after_persist,
    wrap_tool_usage_notice_user_message,
)
from message_policy.tool_usage_notice.state import persist_tool_usage_notice_state


def apply_tool_usage_notice_before_llm(
    messages: list[dict[str, Any]],
    state: Any,
    ctx: Any,
    meta: dict[str, Any],
    *,
    step_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Цикл §4.4 после persist-batch, перед вызовом LLM: K-тик, tool usage notices, long hints.
    """
    cfg = getattr(ctx, "config_dict", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    notice_rules = active_tool_usage_notice_rules(cfg)

    if long_memory_hints_enabled(cfg):
        on_persist_steps(meta, steps=1)

    if notice_rules:
        update_tool_usage_notice_after_persist(state, step_messages, notice_rules)

    out = list(messages)
    emitted_any_technical_notice = False

    for hint_text in collect_long_memory_hints(state, meta, ctx, cfg):
        out.append(wrap_long_memory_hint_user_message(hint_text, suffix="mem_long"))
        emitted_any_technical_notice = True

    shared = getattr(state, "shared", None)
    registers = get_thread_memory_registers_from_shared(shared)

    for notice_text in collect_tool_usage_notices(state, notice_rules, registers=registers):
        out.append(wrap_tool_usage_notice_user_message(notice_text))
        emitted_any_technical_notice = True

    if notice_rules and emitted_any_technical_notice:
        update_tool_usage_notice_after_persist(
            state,
            step_messages,
            notice_rules,
            reset_tool_names={rule.tool_name for rule in notice_rules},
        )

    persist_tool_usage_notice_state(state, meta)
    return out
