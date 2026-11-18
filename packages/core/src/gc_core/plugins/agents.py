"""Субагенты плагинов: ``agents/*.md`` в определения кастомных субагентов
(§6).

Перевод делается при сборке снимка набора (``plugins.catalog``); в снимок
кладётся готовое ``SubagentDefinition``. Правило инструментов (§6.2):

* ключа ``tools`` нет — все инструменты;
* ``tools`` пуст (или ``null`` / неверного типа, с диагностикой) — только
  служебные; субагент подключается;
* непустой список — переведённое, отброшенное в диагностику; не перевелось
  ничего — субагент не подключается;
* битая шапка — субагент не подключается.

Имена (§6.3): таблица ``TOOL_ALIASES``; ``snake_case`` — только встроенный
инструмент агента; ``mcp__<сервер>[__<тул>|__*]`` и
``mcp__plugin_<плагин>_<сервер>__<тул>`` — инструменты серверов этого же
плагина, парой «(ключ сервера, инструмент или None = все)».
"""

from __future__ import annotations

import re
from typing import Mapping

from gc_core.constants.tool_names import shorten_tool_name
from gc_core.plugins.manifest import Plugin, PluginAgent
from gc_core.tools.subagents.custom.file_definitions import (
    SubagentDefinition,
)

# Инструменты формата Claude Code в инструменты агента.
TOOL_ALIASES: dict[str, str] = {
    "Read": "fs_read_file",
    "Write": "fs_write_file",
    "Edit": "fs_apply_file_edits",
    "MultiEdit": "fs_apply_file_edits",
    "Grep": "fs_find_in_files",
    "Glob": "fs_find_files_by_name",
    "LS": "fs_list_files",
    "Bash": "exec_command",
    "WebFetch": "exec_fetch_url",
    "TodoWrite": "session_todo",
}

_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
_MCP_PREFIX = "mcp__"
_MAX_DESCRIPTION_LEN = 1024
# ``model: inherit`` — модель родителя, то есть ровно то, что и так будет.
_MODEL_INHERIT = "inherit"

McpAllow = tuple[str, str | None]
_Keys = Mapping[tuple[str, str], str]


def _safe(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_") or "x"


def subagent_name(plugin: str, agent: str) -> str:
    """Имя инструмента субагента: ``subagent_<плагин>_<агент>``, до 64 знаков."""
    return shorten_tool_name(f"subagent_{_safe(plugin)}_{_safe(agent)}")


def _mcp_pair(raw: str, plugin: str, mcp_keys: _Keys) -> McpAllow | None:
    """MCP-имя субагента в пару «(ключ, инструмент или None)»; сервер не этого
    плагина или не в действующих — ``None``."""
    server, _, tool = raw[len(_MCP_PREFIX) :].partition("__")
    prefixed = f"plugin_{plugin}_"
    key = mcp_keys.get((plugin, server))
    if key is None and server.startswith(prefixed):
        key = mcp_keys.get((plugin, server[len(prefixed) :]))
    if key is None:
        return None
    return key, (tool if tool not in ("", "*") else None)


def _builtin_name(raw: str, builtin: set[str]) -> str | None:
    target = TOOL_ALIASES.get(raw)
    if target is None and _SNAKE.match(raw) and raw in builtin:
        target = raw
    return target


def _translate(
    names: tuple[str, ...], plugin: str, builtin: set[str], mcp_keys: _Keys
) -> tuple[tuple[str, ...], tuple[McpAllow, ...], list[str]]:
    """Имена субагента: (встроенные, MCP-пары, отброшенные)."""
    tools: dict[str, None] = {}
    mcp: dict[McpAllow, None] = {}
    dropped: list[str] = []
    for raw in names:
        pair = (
            _mcp_pair(raw, plugin, mcp_keys)
            if raw.startswith(_MCP_PREFIX)
            else None
        )
        target = _builtin_name(raw, builtin) if pair is None else None
        if pair is not None:
            mcp[pair] = None
        elif target is not None:
            tools[target] = None
        else:
            dropped.append(raw)
    return tuple(tools), tuple(mcp), dropped


def _description(plugin: Plugin, agent: PluginAgent) -> str:
    text = agent.description or f"Субагент плагина {plugin.name}: {agent.name}"
    if len(text) > _MAX_DESCRIPTION_LEN:
        text = text[: _MAX_DESCRIPTION_LEN - 1].rstrip() + "…"
    return text


def _listed_scope(
    plugin: str,
    agent: PluginAgent,
    label: str,
    builtin: set[str],
    mcp_keys: _Keys,
) -> tuple[tuple[str, ...], tuple[McpAllow, ...], list[str]] | None:
    """Область субагента с непустым списком; ``None`` — не перевелось
    ничего."""
    tools, mcp, dropped = _translate(agent.tools, plugin, builtin, mcp_keys)
    if not tools and not mcp:
        return None
    notices = (
        [f"Субагент {label}: инструменты отброшены: {', '.join(dropped)}."]
        if dropped
        else []
    )
    return tools, mcp, notices


def translate_role(
    plugin: Plugin,
    agent: PluginAgent,
    *,
    builtin: set[str],
    mcp_keys: _Keys,
) -> tuple[SubagentDefinition | None, tuple[str, ...]]:
    """Субагент плагина в определение субагента и его диагностика.

    ``builtin`` — имена встроенных инструментов агента
    (``all_builtin_tool_names``), ``mcp_keys`` — «(плагин, сервер) →
    действующий ключ» из снимка. ``None`` — субагент не подключается."""
    label = f"{plugin.name}:{agent.name}"
    if agent.header_broken:
        return None, (f"Субагент {label} не подключён: {agent.problem}.",)
    notices: list[str] = []
    if agent.model and agent.model != _MODEL_INHERIT:
        notices.append(
            f"Субагент {label}: поле model проигнорировано, модель "
            "субагента задаёт реестр моделей."
        )
    tools: tuple[str, ...] | None = agent.tools
    mcp: tuple[McpAllow, ...] = ()
    if agent.problem:
        notices.append(
            f"Субагент {label}: {agent.problem}; доступны только служебные "
            "инструменты."
        )
    elif agent.tools:
        scope = _listed_scope(plugin.name, agent, label, builtin, mcp_keys)
        if scope is None:
            notices.append(
                f"Субагент {label} не подключён: ни один инструмент из списка "
                f"не переведён ({', '.join(agent.tools)})."
            )
            return None, tuple(notices)
        tools, mcp, dropped = scope
        notices += dropped
    return SubagentDefinition(
        name=subagent_name(plugin.name, agent.name),
        description=_description(plugin, agent),
        role=agent.body,
        tools=tools,
        stop_tools=(),
        source_path=agent.source,
        mcp_allow=mcp,
    ), tuple(notices)
