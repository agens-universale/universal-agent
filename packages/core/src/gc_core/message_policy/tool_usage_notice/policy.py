from __future__ import annotations

from typing import Any

from gc_core.config_modules.message_notice import ToolUsageNoticeRule, active_tool_usage_notice_rules, tool_usage_notice_policy_enabled
from gc_core.message_policy.user_message_policy import wrap_technical_user_content, TECHNICAL_TYPE_CURRENT_CONTEXT
from gc_core.utils.generate_message_id import generate_msg_id_sha256 as generate_message_id


def batch_used_tool(step_messages: list[dict[str, Any]], tool_name: str) -> bool:
    target = str(tool_name or "").strip()
    if not target:
        return False
    for message in step_messages or []:
        if not isinstance(message, dict):
            continue
        name = message.get("name")
        if name == target:
            return True
        if message.get("type") == "function_call" and name == target:
            return True
    return False


def _get_tool_usage_notice_counters(state: Any) -> dict[str, int]:
    raw = getattr(state, "tool_usage_notice_cnt", None)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            out[key.strip()] = parsed
    return out


def _set_tool_usage_notice_counters(state: Any, counters: dict[str, int]) -> None:
    state.tool_usage_notice_cnt = dict(counters)


def update_tool_usage_notice_after_persist(
    state: Any,
    step_messages: list[dict[str, Any]],
    rules: list[ToolUsageNoticeRule],
    *,
    reset_tool_names: set[str] | None = None,
) -> None:
    """+1 за persist-batch без вызова tool; сброс для tool из reset_tool_names или batch."""
    reset_tool_names = reset_tool_names or set()
    counters = _get_tool_usage_notice_counters(state)
    for rule in rules:
        tool_name = rule.tool_name
        if tool_name in reset_tool_names or batch_used_tool(step_messages, tool_name):
            counters[tool_name] = 0
            continue
        counters[tool_name] = counters.get(tool_name, 0) + 1
    _set_tool_usage_notice_counters(state, counters)


def _format_register_index(registers: dict[str, str]) -> str:
    lines: list[str] = []
    for name in sorted(registers):
        content = registers.get(name)
        if not isinstance(content, str) or not content.strip():
            continue
        chars = len(content)
        line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
        lines.append(f"- {name}: {chars} chars, {line_count} lines")
    if not lines:
        return "No non-empty registers in thread memory."
    return "Registers:\n" + "\n".join(lines)


def collect_tool_usage_notices(
    state: Any,
    rules: list[ToolUsageNoticeRule],
    *,
    registers: dict[str, str] | None = None,
    append_register_index: bool = False,
) -> list[str]:
    counters = _get_tool_usage_notice_counters(state)
    emitted: list[str] = []
    reset_names: set[str] = set()
    reg = registers if isinstance(registers, dict) else {}
    for rule in rules:
        threshold = rule.step_messages_count
        if counters.get(rule.tool_name, 0) < threshold:
            continue
        text = rule.notice_message
        if append_register_index and reg:
            index = _format_register_index(reg)
            text = f"{text.rstrip()}\n{index}"
        emitted.append(text)
        reset_names.add(rule.tool_name)
        counters[rule.tool_name] = 0
    if reset_names:
        _set_tool_usage_notice_counters(state, counters)
    return emitted


def build_subagent_thread_memory_bootstrap_message(registers: dict[str, str] | None) -> str:
    reg = registers if isinstance(registers, dict) else {}
    non_empty = {
        name: content
        for name, content in reg.items()
        if isinstance(content, str) and content.strip()
    }
    if not non_empty:
        return (
            "Thread memory: no registers yet. "
            "Append durable facts with the thread memory tool before submit when you learn "
            "something worth keeping."
        )
    index = _format_register_index(non_empty)
    return (
        "Thread memory is shared with the orchestrator for this thread. "
        "Read registers you need; update persistent registers with the thread memory tool "
        "before submit when outcomes should persist.\n"
        f"{index}"
    )


def get_thread_memory_registers_from_shared(shared: Any) -> dict[str, str]:
    if shared is None:
        return {}

    raw = getattr(shared, "working_memory", None)
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): content
        for name, content in raw.items()
        if isinstance(name, str) and isinstance(content, str)
    }


def subagent_thread_memory_bootstrap_text(
    config_dict: dict[str, Any] | None,
    registers: dict[str, str] | None,
) -> str | None:
    """Текст one-shot bootstrap для subagent при старте; без meta/state side effects."""
    if not tool_usage_notice_policy_enabled(config_dict):
        return None
    return build_subagent_thread_memory_bootstrap_message(registers)


def wrap_thread_memory_technical_user_message(
    text: str,
    *,
    message_id_suffix: str = "mem_gate",
) -> dict[str, Any]:
    return {
        "role": "user",
        "content": wrap_technical_user_content(
            text,
            technical_type=TECHNICAL_TYPE_CURRENT_CONTEXT,
        ),
        "id": generate_message_id(f"m_{message_id_suffix}"),
    }


def wrap_tool_usage_notice_user_message(text: str) -> dict[str, Any]:
    return wrap_thread_memory_technical_user_message(text, message_id_suffix="tool_usage_notice")
