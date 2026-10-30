"""Чтение набора плагинов из настроек — остаток до перевода потребителей на
снимок (``RuntimeEnv.plugins``); модуль уходит в задаче уборки.

Хуки для цикла агента — ``gc_core.plugins.hooks``.
"""

from __future__ import annotations

from typing import Any

from gc_core.plugins.catalog import load_plugins, plugin_entries
from gc_core.plugins.manifest import Plugin


def _plugins_config(ctx: Any) -> Any | None:
    if ctx is None:
        return None
    try:
        from gc_core.settings import settings_of

        return settings_of(ctx).plugins
    except Exception:  # плагины не должны ронять агента
        return None


def configured_paths(cfg: Any, *, fetch: bool = False) -> list[str]:
    """Каталоги плагинов набора (``catalog.plugin_entries``); git-источники
    клонируются только при ``fetch=True``, иначе берётся кеш."""
    if cfg is None:
        return []
    return list(plugin_entries(cfg, clone=fetch).paths)


def active_plugins(ctx: Any) -> tuple[Plugin, ...]:
    if getattr(ctx, "durable_execution", False):
        return ()
    cfg = _plugins_config(ctx)
    paths = configured_paths(cfg)
    return load_plugins(paths) if paths else ()
