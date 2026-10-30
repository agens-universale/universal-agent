"""Доступность роли и её права — без раннера.

Для сводки установки плагина и карточки ``/plugins``: показать, почему роль
сейчас не вызвать и что ей разрешено, не запуская субагента. Решения те же,
что у реестра сабагентов (``registry``) и дискавери определений.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gc_core.security.permission_mode import CAT_EDIT, CAT_EXEC
from gc_core.settings import runtime_of, settings_of
from gc_core.threads.facade import run_sync
from gc_core.tools.registry import get_builtin_tools_data
from gc_core.tools.subagents.custom.file_definitions import (
    SubagentDefinition,
    adiscover_subagent_definitions,
)
from gc_core.tools.subagents.registry import (
    builtin_subagent_names,
    resolve_subagent_definitions,
)
from gc_core.tools.subagents.selection import (
    subagents_allowlist,
    subagents_enabled,
)

_CHAT_COMPLETIONS = "chat_completions"


@dataclass(frozen=True)
class RoleRights:
    """Что роли разрешено: правка файлов, команды, все тулы, MCP."""

    edit: bool
    exec: bool
    all_tools: bool
    mcp: bool


def _taken_reason(settings: Any, name: str) -> str | None:
    """Имя занято встроенным или определением из папки (оно читается первым)."""
    if name in builtin_subagent_names():
        return f"имя {name} занято встроенным субагентом"
    own = next(
        (r for r in runtime_of(settings).plugins.roles if r.name == name),
        None,
    )
    found = next(
        (
            d
            for d in run_sync(
                adiscover_subagent_definitions(
                    settings, builtin_names=builtin_subagent_names()
                )
            )
            if d.name == name
        ),
        None,
    )
    if (
        own is not None
        and found is not None
        and found.source_path != own.source_path
    ):
        return f"имя {name} занято определением из {found.source_path}"
    return None


def role_availability(settings: Any, name: str) -> str | None:
    """Почему роль ``name`` сейчас недоступна; ``None`` — доступна."""
    agent = settings_of(settings).agent
    if not subagents_enabled(agent.use_local_subagents):
        return "субагенты выключены: включите agent.use_local_subagents"
    if settings_of(settings).backend.api_mode != _CHAT_COMPLETIONS:
        return "субагенты работают только в режиме chat_completions"
    taken = _taken_reason(settings, name)
    if taken:
        return taken
    allowed = subagents_allowlist(agent.use_local_subagents)
    if allowed is not None and name not in allowed:
        return (
            f"роли нет в agent.use_local_subagents: добавьте {name} в список"
        )
    if name not in resolve_subagent_definitions(settings):
        return (
            "превышен agent.subagents_max_count: роль не поместилась "
            "в лимит, увеличьте его"
        )
    return None


def _has_category(
    registry: dict[str, dict[str, Any]], names: Any, category: str
) -> bool:
    """Есть ли среди ``names`` (``None`` — все) тул категории."""
    return any(
        entry.get("category") == category
        for name, entry in registry.items()
        if names is None or name in names
    )


def role_rights(settings: Any, definition: SubagentDefinition) -> RoleRights:
    """Права роли по категориям встроенных тулов и ``mcp_allow``.

    ``tools is None`` — доступны все тулы: правка и команды тогда разрешены,
    если такие встроенные тулы вообще есть.
    """
    registry = get_builtin_tools_data(settings)
    names = definition.tools
    return RoleRights(
        edit=_has_category(registry, names, CAT_EDIT),
        exec=_has_category(registry, names, CAT_EXEC),
        all_tools=names is None,
        mcp=bool(definition.mcp_allow),
    )
