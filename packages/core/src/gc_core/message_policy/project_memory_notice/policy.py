from __future__ import annotations

from typing import Any, Callable

from gc_core.constants.tool_names import PROJECT_MEMORY_TOOL_NAME
from gc_core.message_policy.tool_usage_notice.state import (
    get_project_memory_check_cnt,
    set_project_memory_check_cnt,
)
from gc_core.settings import settings_of


def tool_project_memory_enabled(settings: Any) -> bool:
    """Тул project_memory не в ``agent.stop_tools``.

    ``settings`` здесь и ниже — то, что принимает ``settings_of`` (ctx,
    ``LoadedSettings``, ``AppConfig``).
    """
    return (
        PROJECT_MEMORY_TOOL_NAME not in settings_of(settings).agent.stop_tools
    )


def project_memory_hints_enabled(settings: Any) -> bool:
    """Exists/changed hints: только явный флаг в ``project_memory`` и живой тул."""
    settings = settings_of(settings)
    return bool(
        settings.project_memory.enable_project_memory_notice
    ) and tool_project_memory_enabled(settings)


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
    get_hash_fn: Callable[[Any], str | None],
) -> str | None:
    """Обновить baseline после read/write/append project_memory tool.

    ``get_hash_fn(ctx)`` — версия памяти, которую тул только что прочитал
    или записал (читать её здесь заново не нужно)."""
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
    settings: Any,
    *,
    get_hash_fn: Callable[[Any], str | None],
) -> list[str]:
    """Exists/changed hints на K-тике; пустой список если tool/mode выключены.

    ``get_hash_fn(ctx)`` — текущая версия памяти; её читают заранее,
    асинхронно (``project_memory.aget_hash``): цепочка подсказок синхронная.
    """
    settings = settings_of(settings)
    if not project_memory_hints_enabled(settings):
        return []

    interval = settings.project_memory.check_interval_messages
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
