from __future__ import annotations

import json
from typing import Any

from gc_core.config_modules.message_notice import ToolUsageNoticeRule
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_CURRENT_CONTEXT,
    wrap_technical_user_content,
)
from gc_core.settings import settings_of
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id,
)


def _output_payload(message: dict[str, Any]) -> dict[str, Any] | None:
    """JSON ответа тула. Flush перед моделью видит function_call_output, не call."""
    if message.get("type") not in {"function_call_output", "tool_call_output"}:
        return None
    raw = message.get("output")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or raw[:1] != "{":
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _invoked_tool_name(message: dict[str, Any]) -> str:
    """Имя тула, который вызвали в этом сообщении.

    У call это ``name``. У output — поле ``tool`` тела. У свёрнутого
    stored-envelope ``tool`` нет, имя в ``source_tool``. Страница чтения
    ставит ``tool`` на reader, а ``source_tool`` — на исходный тул: считать
    нужно вызов, а не то, чью страницу читают.
    """
    name = message.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    payload = _output_payload(message)
    if payload is None:
        return ""
    tool = payload.get("tool")
    if isinstance(tool, str) and tool.strip():
        return tool.strip()
    source = payload.get("source_tool")
    if isinstance(source, str) and source.strip():
        return source.strip()
    return ""


def batch_used_tool(
    step_messages: list[dict[str, Any]], tool_name: str
) -> bool:
    target = str(tool_name or "").strip()
    if not target:
        return False
    for message in step_messages or []:
        if isinstance(message, dict) and _invoked_tool_name(message) == target:
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


def _set_tool_usage_notice_counters(
    state: Any, counters: dict[str, int]
) -> None:
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
        if tool_name in reset_tool_names or batch_used_tool(
            step_messages, tool_name
        ):
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


def build_subagent_thread_memory_bootstrap_message(
    registers: dict[str, str] | None,
) -> str:
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


def active_tool_usage_notice_rules(
    settings: Any,
) -> list[ToolUsageNoticeRule]:
    """Правила ``tool_usage_notice`` без тулов из ``agent.stop_tools``.

    ``settings`` — то, что принимает ``settings_of`` (ctx, ``LoadedSettings``,
    ``AppConfig``). Правила уже нормализованы при загрузке настроек.
    """
    settings = settings_of(settings)
    stopped = {
        str(item).strip()
        for item in settings.agent.stop_tools
        if isinstance(item, str) and item.strip()
    }
    return [
        rule
        for rule in settings.tool_usage_notice
        if rule.tool_name not in stopped
    ]


def subagent_thread_memory_bootstrap_text(
    settings: Any,
    registers: dict[str, str] | None,
) -> str | None:
    """Текст one-shot bootstrap для subagent при старте; без meta/state side effects.

    ``settings`` — то, что принимает ``settings_of``.
    """
    if not active_tool_usage_notice_rules(settings):
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
    return wrap_thread_memory_technical_user_message(
        text, message_id_suffix="tool_usage_notice"
    )
