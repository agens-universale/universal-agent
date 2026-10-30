"""Чтение набора плагинов из настроек — остаток до перевода потребителей на
снимок (``RuntimeEnv.plugins``); модуль уходит в задаче уборки.

Хуки для цикла агента — ``gc_core.plugins.hooks``.
"""

from __future__ import annotations

from typing import Any

from gc_core.plugins.catalog import load_plugins, plugin_entries

# ``load_plugins`` экран ``/plugins`` в TUI берёт отсюда до перевода на снимок.
__all__ = ["configured_paths", "load_plugins"]


def configured_paths(cfg: Any, *, fetch: bool = False) -> list[str]:
    """Каталоги плагинов набора (``catalog.plugin_entries``); git-источники
    клонируются только при ``fetch=True``, иначе берётся кеш."""
    if cfg is None:
        return []
    return list(plugin_entries(cfg, clone=fetch).paths)
