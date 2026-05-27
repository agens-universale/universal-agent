from __future__ import annotations

from typing import Any

from config_modules.memory_messages import (
    memory_file_notice_enabled,
    memory_gate_from_config_dict,
)
from message_policy.memory_messages_notice.state import (
    get_subagent_thread_memory_announced,
    set_subagent_thread_memory_announced,
)
from message_policy.user_message_policy import wrap_technical_user_content, TECHNICAL_TYPE_CURRENT_CONTEXT

THREAD_MEMORY_TOOL_NAMES = (
    "working_memory",
    "memory_files",
)


def _stopped_tools(config_dict: dict[str, Any] | None) -> set[str]:
    stop = config_dict.get("stop_tools") if isinstance(config_dict, dict) else None
    if not isinstance(stop, list):
        return set()
    return {str(item).strip() for item in stop if isinstance(item, str) and item.strip()}


def preferred_thread_memory_tool_name(config_dict: dict[str, Any] | None) -> str:
    stopped = _stopped_tools(config_dict)
    for tool_name in THREAD_MEMORY_TOOL_NAMES:
        if tool_name not in stopped:
            return tool_name
    return THREAD_MEMORY_TOOL_NAMES[0]


def tool_memory_files_enabled(config_dict: dict[str, Any] | None) -> bool:
    stopped = _stopped_tools(config_dict)
    return any(tool_name not in stopped for tool_name in THREAD_MEMORY_TOOL_NAMES)


def memory_gate_enabled(config_dict: dict[str, Any] | None) -> bool:
    return (
        memory_file_notice_enabled(config_dict)
        and tool_memory_files_enabled(config_dict)
    )


def batch_used_memory_files(step_messages: list[dict[str, Any]]) -> bool:
    for message in step_messages or []:
        if not isinstance(message, dict):
            continue
        name = message.get("name")
        if name in THREAD_MEMORY_TOOL_NAMES:
            return True
        if message.get("type") == "function_call" and name in THREAD_MEMORY_TOOL_NAMES:
            return True
    return False


def update_memory_notice_after_persist(
    state: Any,
    step_messages: list[dict[str, Any]],
    *,
    emitted_memory_notice: bool = False,
) -> None:
    """+1 за persist-batch без memory_files; сброс при tool или после emit gate."""
    if emitted_memory_notice or batch_used_memory_files(step_messages):
        state.memory_notice_cnt = 0
        return
    state.memory_notice_cnt = int(getattr(state, "memory_notice_cnt", 0) or 0) + 1


def _format_register_index(memory_files: dict[str, str]) -> str:
    lines: list[str] = []
    for name in sorted(memory_files):
        content = memory_files.get(name)
        if not isinstance(content, str) or not content.strip():
            continue
        chars = len(content)
        line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
        lines.append(f"- {name}: {chars} chars, {line_count} lines")
    if not lines:
        return "No non-empty registers in thread wiki."
    return "Registers:\n" + "\n".join(lines)


def build_memory_gate_message(
    *,
    cooldown_messages: int,
    memory_files: dict[str, str],
    tool_name: str,
) -> str:
    index = _format_register_index(memory_files if isinstance(memory_files, dict) else {})
    return (
        f"You have not used {tool_name} for at least {cooldown_messages} steps. "
        "Review the thread wiki if you need persistent context. "
        f"Prefer dialog_summary when present; use {tool_name} read when needed.\n"
        f"{index}"
    )


def maybe_emit_memory_gate(
    state: Any,
    config_dict: dict[str, Any] | None,
    memory_files: dict[str, str] | None,
) -> str | None:
    if not memory_gate_enabled(config_dict):
        return None
    gate_cfg = memory_gate_from_config_dict(config_dict)
    threshold = gate_cfg.cooldown_messages
    if int(getattr(state, "memory_notice_cnt", 0) or 0) < threshold:
        return None
    return build_memory_gate_message(
        cooldown_messages=threshold,
        memory_files=memory_files or {},
        tool_name=preferred_thread_memory_tool_name(config_dict),
    )


def build_subagent_wiki_bootstrap_message(memory_files: dict[str, str] | None) -> str:
    mf = memory_files if isinstance(memory_files, dict) else {}
    non_empty = {
        name: content
        for name, content in mf.items()
        if isinstance(content, str) and content.strip()
    }
    if not non_empty:
        return (
            "Thread memory (shared wiki): no registers yet. "
            "Append durable facts with the thread memory tool before submit when you learn something worth keeping."
        )
    index = _format_register_index(non_empty)
    return (
        "Thread memory (shared wiki): shared with the orchestrator for this thread. "
        "Read registers you need; append durable outcomes with the thread memory tool before submit.\n"
        f"{index}"
    )


def get_thread_memory_registers_from_shared(shared: Any) -> dict[str, str]:
    if shared is None:
        return {}

    for attr_name in ("working_memory", "memory_files"):
        raw = getattr(shared, attr_name, None)
        if not isinstance(raw, dict):
            continue
        out = {
            str(name): content
            for name, content in raw.items()
            if isinstance(name, str) and isinstance(content, str)
        }
        if out:
            return out
    return {}


def maybe_emit_subagent_wiki_bootstrap(
    meta: dict[str, Any],
    config_dict: dict[str, Any] | None,
    memory_files: dict[str, str] | None,
) -> str | None:
    if not memory_gate_enabled(config_dict):
        return None
    if get_subagent_thread_memory_announced(meta):
        return None
    set_subagent_thread_memory_announced(meta, True)
    return build_subagent_wiki_bootstrap_message(memory_files)


def wrap_thread_memory_technical_user_message(
    text: str,
    *,
    message_id_suffix: str = "mem_gate",
) -> dict[str, Any]:
    from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id

    return {
        "role": "user",
        "content": wrap_technical_user_content(
            text,
            technical_type=TECHNICAL_TYPE_CURRENT_CONTEXT,
        ),
        "id": generate_message_id(f"m_{message_id_suffix}"),
    }


def wrap_memory_gate_user_message(text: str) -> dict[str, Any]:
    return wrap_thread_memory_technical_user_message(text, message_id_suffix="mem_gate")
