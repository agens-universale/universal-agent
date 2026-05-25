from __future__ import annotations

from typing import Any

from message_policy.long_memory_notice.policy import (
    collect_long_memory_hints,
    long_memory_hints_enabled,
    on_persist_steps,
    wrap_long_memory_hint_user_message,
)
from message_policy.memory_messages_notice.policy import (
    memory_gate_enabled,
    maybe_emit_memory_gate,
    update_memory_notice_after_persist,
    wrap_memory_gate_user_message,
)
from message_policy.memory_messages_notice.state import persist_memory_messages_state


def apply_memory_messages_before_llm(
    messages: list[dict[str, Any]],
    state: Any,
    ctx: Any,
    meta: dict[str, Any],
    *,
    step_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Цикл §4.4 после persist-batch, перед вызовом LLM: K-тик, wiki gate, long hints.
    """
    cfg = getattr(ctx, "config_dict", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    if long_memory_hints_enabled(cfg):
        on_persist_steps(meta, steps=1)

    if memory_gate_enabled(cfg):
        update_memory_notice_after_persist(state, step_messages)

    out = list(messages)
    emitted_notice = False

    for hint_text in collect_long_memory_hints(state, meta, ctx, cfg):
        out.append(wrap_long_memory_hint_user_message(hint_text, suffix="mem_long"))
        emitted_notice = True

    memory_files = {}
    shared = getattr(state, "shared", None)
    if shared is not None:
        raw_mf = getattr(shared, "memory_files", None)
        if isinstance(raw_mf, dict):
            memory_files = raw_mf

    gate_text = maybe_emit_memory_gate(state, cfg, memory_files)
    if gate_text:
        out.append(wrap_memory_gate_user_message(gate_text))
        emitted_notice = True

    if memory_gate_enabled(cfg) and emitted_notice:
        update_memory_notice_after_persist(
            state, step_messages, emitted_memory_notice=True
        )

    persist_memory_messages_state(state, meta)
    return out
