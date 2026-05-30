from __future__ import annotations

from typing import Any, Callable

from config_modules.message_notice import (
    long_memory_notice_from_config_dict,
    long_memory_notice_enabled,
)
from message_policy.tool_usage_notice.state import (
    get_long_memory_check_cnt,
    set_long_memory_check_cnt,
)
from message_policy.user_message_policy import wrap_technical_user_content, TECHNICAL_TYPE_CURRENT_CONTEXT


def tool_long_memory_enabled(config_dict: dict[str, Any] | None) -> bool:
    stop = config_dict.get("stop_tools") if isinstance(config_dict, dict) else None
    if isinstance(stop, list) and "long_memory" in stop:
        return False
    return True


def long_memory_hints_enabled(config_dict: dict[str, Any] | None) -> bool:
    return (
        long_memory_notice_enabled(config_dict)
        and tool_long_memory_enabled(config_dict)
    )


def on_persist_steps(meta: dict[str, Any], *, steps: int = 1) -> int:
    """Увеличить K-счётчик в meta; вернуть новое значение."""
    increment = max(1, int(steps or 1))
    new_cnt = get_long_memory_check_cnt(meta) + increment
    set_long_memory_check_cnt(meta, new_cnt)
    return new_cnt


def touch_long_memory_baseline(
    state: Any,
    ctx: Any,
    *,
    get_hash_fn: Callable[[Any], str | None] | None = None,
) -> str | None:
    """Обновить baseline после read/write/append long_memory tool."""
    if get_hash_fn is None:
        from long_memory import get_hash

        get_hash_fn = get_hash
    current = get_hash_fn(ctx)
    state.long_memory_content_hash = current
    return current


def build_long_memory_exists_message() -> str:
    return (
        "This workspace has long memory (project-scoped markdown). "
        "Use the long_memory tool with action=read when conventions, "
        "decisions, or durable facts may matter — not on every turn."
    )


def build_long_memory_changed_message() -> str:
    return (
        "Project long memory was updated since your last recorded baseline. "
        "Call long_memory with action=read if you need the current content."
    )


def collect_long_memory_hints(
    state: Any,
    meta: dict[str, Any],
    ctx: Any,
    config_dict: dict[str, Any] | None,
    *,
    get_hash_fn: Callable[[Any], str | None] | None = None,
) -> list[str]:
    """Exists/changed hints на K-тике; пустой список если tool/mode выключены."""
    if not long_memory_hints_enabled(config_dict):
        return []
    if get_hash_fn is None:
        from long_memory import get_hash

        get_hash_fn = get_hash

    cfg = long_memory_notice_from_config_dict(config_dict)
    interval = cfg.check_interval_messages
    if get_long_memory_check_cnt(meta) < interval:
        return []

    set_long_memory_check_cnt(meta, 0)

    current = get_hash_fn(ctx)
    baseline = getattr(state, "long_memory_content_hash", None)

    hints: list[str] = []
    if current is not None and baseline is None:
        hints.append(build_long_memory_exists_message())
        state.long_memory_content_hash = current
    elif (
        current is not None
        and isinstance(baseline, str)
        and current != baseline
    ):
        hints.append(build_long_memory_changed_message())
    return hints


def wrap_long_memory_hint_user_message(text: str, *, suffix: str = "mem_long") -> dict[str, Any]:
    from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id

    return {
        "role": "user",
        "content": wrap_technical_user_content(
            text,
            technical_type=TECHNICAL_TYPE_CURRENT_CONTEXT,
        ),
        "id": generate_message_id(f"m_{suffix}"),
    }
