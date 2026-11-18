"""Снимок набора плагинов — неизменяемые данные для ``RuntimeEnv``.

Собирает его ``gc_core.plugins.catalog``; потребители читают готовые поля
через ``runtime_of(...)``, а модуль сборки не импортируют.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from gc_core.config_modules.mcp import McpServerConfig
from gc_core.plugins.manifest import Plugin, PluginCommand


@dataclass(frozen=True)
class SkillRoot:
    """Каталог skills плагина; ``contain`` — корень проверки вложенности."""

    plugin: str
    path: str
    contain: str | None


@dataclass(frozen=True)
class PluginSnapshot:
    plugins: tuple[Plugin, ...] = ()
    #: Плагины, чьи хуки можно запускать.
    trusted: frozenset[str] = frozenset()
    skill_roots: tuple[SkillRoot, ...] = ()
    commands: tuple[tuple[str, PluginCommand], ...] = ()
    mcp_servers: Mapping[str, McpServerConfig] = field(default_factory=dict)
    mcp_keys: Mapping[tuple[str, str], str] = field(default_factory=dict)
    roles: tuple[Any, ...] = ()
    notices: tuple[str, ...] = ()
    fingerprint: tuple = ()
