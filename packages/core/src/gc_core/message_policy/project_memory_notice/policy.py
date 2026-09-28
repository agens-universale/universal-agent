from __future__ import annotations

from typing import Any, Callable

from gc_core.config_modules.project_memory import (
    project_memory_from_config_dict,
    project_memory_notice_enabled,
)
from gc_core.constants.tool_names import PROJECT_MEMORY_TOOL_NAME
from gc_core.message_policy.tool_usage_notice.state import (
    get_project_memory_check_cnt,
    set_project_memory_check_cnt,
)
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_CURRENT_CONTEXT,
    wrap_technical_user_content,
)
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id,
)


def tool_project_memory_enabled(config_dict: dict[str, Any] | None) -> bool:
    stop = (
        config_dict.get("stop_tools")
        if isinstance(config_dict, dict)
        else None
    )
    return not (isinstance(stop, list) and PROJECT_MEMORY_TOOL_NAME in stop)


def project_memory_hints_enabled(config_dict: dict[str, Any] | None) -> bool:
    return project_memory_notice_enabled(
        config_dict
    ) and tool_project_memory_enabled(config_dict)


def on_persist_steps(meta: dict[str, Any], *, steps: int = 1) -> int:
    """Увеличить K-счётчик в meta; вернуть новое значение."""
    increment = max(1, int(steps or 1))
    new_cnt = get_project_memory_check_cnt(meta) + increment
    set_project_memory_check_cnt(meta, new_cnt)
    return new_cnt


def touch_project_memory_baseline(
    state: Any,
    ctx: Any,
    *,
    get_hash_fn: Callable[[Any], str | None] | None = None,
) -> str | None:
    """Обновить baseline после read/write/append project_memory tool."""
    if get_hash_fn is None:
        from gc_core.project_memory import (
            get_hash,  # nested: tests monkeypatch project_memory.get_hash; runtime lookup required
        )

        get_hash_fn = get_hash
    current = get_hash_fn(ctx)
    state.project_memory_content_hash = current
    return current


def build_project_memory_exists_message() -> str:
    return (
        "This workspace has project memory (project-scoped markdown). "
        "Use the project memory tool with action=read when conventions, "
        "decisions, or durable facts may matter — not on every turn."
    )


def build_project_memory_changed_message() -> str:
    return (
        "Project memory was updated since your last recorded baseline. "
        "Call project memory with action=read if you need the current content."
    )


def collect_project_memory_hints(
    state: Any,
    meta: dict[str, Any],
    ctx: Any,
    config_dict: dict[str, Any] | None,
    *,
    get_hash_fn: Callable[[Any], str | None] | None = None,
) -> list[str]:
    """Exists/changed hints на K-тике; пустой список если tool/mode выключены."""
    if not project_memory_hints_enabled(config_dict):
        return []
    if get_hash_fn is None:
        from gc_core.project_memory import (
            get_hash,  # nested: tests monkeypatch project_memory.get_hash; runtime lookup required
        )

        get_hash_fn = get_hash

    cfg = project_memory_from_config_dict(config_dict)
    interval = cfg.check_interval_messages
    if get_project_memory_check_cnt(meta) < interval:
        return []

    set_project_memory_check_cnt(meta, 0)

    current = get_hash_fn(ctx)
    baseline = getattr(state, "project_memory_content_hash", None)

    hints: list[str] = []
    if current is not None and baseline is None:
        hints.append(build_project_memory_exists_message())
        state.project_memory_content_hash = current
    elif (
        current is not None
        and isinstance(baseline, str)
        and current != baseline
    ):
        hints.append(build_project_memory_changed_message())
    return hints


def wrap_project_memory_hint_user_message(
    text: str, *, suffix: str = "mem_project"
) -> dict[str, Any]:
    return {
        "role": "user",
        "content": wrap_technical_user_content(
            text,
            technical_type=TECHNICAL_TYPE_CURRENT_CONTEXT,
        ),
        "id": generate_message_id(f"m_{suffix}"),
    }
