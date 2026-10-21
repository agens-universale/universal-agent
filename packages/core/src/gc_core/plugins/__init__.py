"""Песочница плагинов (экспериментально). См. docs/plugins-playground.md."""

from gc_core.plugins.manifest import Plugin, load_plugin
from gc_core.plugins.runtime import (
    apost_tool_call,
    apre_tool_call,
    asession_start_messages,
    plugin_skill_roots,
)

__all__ = [
    "Plugin",
    "apost_tool_call",
    "apre_tool_call",
    "asession_start_messages",
    "load_plugin",
    "plugin_skill_roots",
]
