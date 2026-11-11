"""Конфиг плагинов.

Плагин это каталог: ``.claude-plugin/plugin.json`` (или ``plugin.json``),
``skills/``, ``commands/``, ``agents/``, ``hooks/hooks.json`` и ``.mcp.json``.
Подключается явным списком: ``paths`` (готовые каталоги) и ``sources``
(git-репозитории, которые агент сам клонирует в кэш), а также установленные
``gc-plugin install`` (``use_installed``). Автопоиска нет. Без плагинов
поведение агента не меняется.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gc_core.config_modules.field_meta import gc_meta

DEFAULT_PLUGIN_HOOK_TIMEOUT_SEC = 30

_SHORT_COMMIT_RE = re.compile(r"^[0-9a-f]{7,39}$")
SHORT_COMMIT_ERROR = (
    "короткий хеш коммита не поддерживается: укажите полный (40 знаков), "
    "ветку или тег"
)


def is_short_commit(ref: str | None) -> bool:
    """Короткий хеш коммита: сервер git отдаёт коммит только по полному."""
    return bool(ref) and _SHORT_COMMIT_RE.match(ref) is not None


class PluginSource(BaseModel):
    """Плагин из git-репозитория."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # URL репозитория или короткая запись ``owner/repo`` (GitHub).
    git: str
    # Ветка, тег или полный хеш коммита; пусто — ветка по умолчанию.
    ref: str | None = None
    # Каталог плагина внутри репозитория (если плагин не в корне).
    subdir: str | None = None

    @field_validator("ref")
    @classmethod
    def _full_commit(cls, ref: str | None) -> str | None:
        if is_short_commit(ref):
            raise ValueError(SHORT_COMMIT_ERROR)
        return ref


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
