"""Субагенты плагинов: ``agents/*.md`` в формат кастомных субагентов агента.

Агент Claude Code (YAML-шапка и текст роли) не проходит разбор кастомных
субагентов как есть: имя должно быть ``subagent_<строчные_латиница_цифры_
подчёркивания>``, допустимы только поля ``name``, ``description``, ``tools``,
``stop_tools``, а инструменты называются по-своему. Здесь файл переводится в
``SubagentDefinition`` так:

* имя: ``subagent_<плагин>_<агент>`` (в нижнем регистре, прочее становится
  ``_``); слишком длинное сокращается и получает хеш;
* ``model``, ``color`` и прочие поля шапки не переносятся;
* ``tools``: имена Claude Code переводятся по таблице; имена, которых в агенте
  нет, отбрасываются с предупреждением; если из непустого списка не осталось
  ничего, агент пропускается (иначе он получил бы больше прав, чем задумал
  автор). Нет ключа ``tools``: доступны все инструменты, как у Claude Code.

Агенты плагинов подхватываются тем же поиском, что и папка
``agent.subagents_dir``, и подчиняются тем же правилам: ``use_local_subagents``,
лимит, коллизии имён (встроенный и прочитанный раньше побеждают).
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Iterable

from gc_core.plugins.manifest import Plugin, PluginAgent

_log = logging.getLogger(__name__)

# Инструменты Claude Code в инструменты агента.
CLAUDE_TOOL_MAP: dict[str, str] = {
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
_MAX_NAME_LEN = 64
_MAX_DESCRIPTION_LEN = 1024


def _safe(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_") or "x"


def subagent_name(plugin: str, agent: str) -> str:
    """Имя инструмента субагента: ``subagent_<плагин>_<агент>``, до 64 знаков."""
    name = f"subagent_{_safe(plugin)}_{_safe(agent)}"
    if len(name) <= _MAX_NAME_LEN:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:6]
    return f"{name[: _MAX_NAME_LEN - 7]}_{digest}"


def map_tools(names: Iterable[str]) -> tuple[tuple[str, ...], list[str]]:
    """Имена инструментов в имена агента: (переведённые, отброшенные)."""
    mapped: list[str] = []
    dropped: list[str] = []
    for raw in names:
        target = CLAUDE_TOOL_MAP.get(raw)
        if target is None and _SNAKE.match(raw):
            target = raw  # уже имя агента (fs_read_file и т.п.)
        if target is None:
            dropped.append(raw)
        elif target not in mapped:
            mapped.append(target)
    return tuple(mapped), dropped


def _warn(
    logger: logging.Logger | None,
    diagnostics: list[dict[str, Any]] | None,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    (logger or _log).warning(message, extra={"event": event, **fields})
    if diagnostics is not None:
        diagnostics.append({"code": event, **fields})


def _definition(
    plugin: Plugin,
    agent: PluginAgent,
    logger: logging.Logger | None,
    diagnostics: list[dict[str, Any]] | None,
) -> Any | None:
    from gc_core.tools.subagents.custom.file_definitions import (
        SubagentDefinition,
    )

    tools: tuple[str, ...] | None = None
    if agent.tools is not None:
        tools, dropped = map_tools(agent.tools)
        if dropped:
            _warn(
                logger,
                diagnostics,
                "plugin_agent_tools_dropped",
                f"plugin_agent_tools_dropped plugin={plugin.name} "
                f"agent={agent.name} tools={','.join(dropped)}",
                plugin=plugin.name,
                agent=agent.name,
                tools=dropped,
            )
        if not tools:
            _warn(
                logger,
                diagnostics,
                "plugin_agent_skipped",
                f"plugin_agent_skipped plugin={plugin.name} agent={agent.name} "
                "(ни один инструмент из списка не найден в агенте)",
                plugin=plugin.name,
                agent=agent.name,
            )
            return None
    description = (
        agent.description or f"Субагент плагина {plugin.name}: {agent.name}"
    )
    if len(description) > _MAX_DESCRIPTION_LEN:
        description = description[: _MAX_DESCRIPTION_LEN - 1].rstrip() + "…"
    return SubagentDefinition(
        name=subagent_name(plugin.name, agent.name),
        description=description,
        role=agent.body,
        tools=tools,
        stop_tools=(),
        source_path=agent.source,
    )


def plugin_subagent_definitions(
    settings: Any,
    *,
    logger: logging.Logger | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """Определения субагентов из ``agents/`` включённых плагинов."""
    from gc_core.plugins.runtime import active_plugins

    out: list[Any] = []
    for plugin in active_plugins(settings):
        for agent in plugin.agents:
            definition = _definition(plugin, agent, logger, diagnostics)
            if definition is not None:
                out.append(definition)
    return out
