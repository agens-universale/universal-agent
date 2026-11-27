"""Конфиг плагинов.

Плагин это каталог: ``.claude-plugin/plugin.json`` (или ``plugin.json``),
``skills/``, ``commands/``, ``agents/``, ``hooks/hooks.json`` и ``.mcp.json``.
Подключается явным списком: ``paths`` (готовые каталоги) и ``sources``
(git-репозитории, которые агент сам клонирует в кэш), а также установленные
``gc-plugin install`` (``use_installed``). Автопоиска нет. Без плагинов
поведение агента не меняется.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from gc_core.config_modules.field_meta import gc_meta

DEFAULT_PLUGIN_HOOK_TIMEOUT_SEC = 30


class PluginSource(BaseModel):
    """Плагин из git-репозитория."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # URL репозитория или короткая запись ``owner/repo`` (GitHub).
    git: str
    # Ветка, тег или полный хеш коммита; пусто — ветка по умолчанию.
    ref: str | None = None
    # Каталог плагина внутри репозитория (если плагин не в корне).
    subdir: str | None = None


class PluginsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: list[PluginSource] = Field(
        default_factory=list, json_schema_extra=gc_meta(expert=True)
    )
    paths: list[str] = Field(
        default_factory=list, json_schema_extra=gc_meta(expert=True)
    )
    # Подключать ли плагины, установленные ``gc-plugin install``. Бенчи
    # выключают: результат прогона не должен зависеть от машины разработчика.
    use_installed: bool = Field(
        default=True, json_schema_extra=gc_meta(expert=True)
    )
    hook_timeout_sec: int = Field(
        default=DEFAULT_PLUGIN_HOOK_TIMEOUT_SEC,
        json_schema_extra=gc_meta(expert=True),
    )
