"""Чтение ресурсов агента ядром: текст, отказы, хранилище контекста.

Тексты отказов одинаковы для любого хранилища и те же, что до v2: путь (или
``вид:имя``) даёт ``store.label``, причина — исходная ошибка.
"""

from __future__ import annotations

import errno
import os
from typing import Any

from gc_core.agent_store.models import (
    ProjectKey,
    ResourceKind,
    needs_project,
)
from gc_core.agent_store.store import AgentStore
from gc_core.utils.user_text import unreadable_reason


def agent_store_of(source: Any) -> AgentStore:
    """Хранилище агента контекста; без него — файловое по настройкам."""
    store = getattr(source, "agent_store", None)
    if store is not None:
        return store
    from gc_core.agent_store.file_store import FileAgentStore

    try:
        return FileAgentStore(source)
    except TypeError:
        # Источник без настроек (заглушка контекста) — настройки по умолчанию.
        return FileAgentStore()


def check_agent_store(store: Any) -> AgentStore:
    """Хранилище встраивающего — проверка на входе, а не на первом чтении."""
    if not isinstance(store, AgentStore):
        raise TypeError(
            "agent_store должен быть AgentStore (например FileAgentStore), "
            f"а не {type(store).__name__}"
        )
    return store


def _configured_project_id(settings: Any) -> str | None:
    if settings is None:
        return None
    from gc_core.settings import settings_of

    try:
        return settings_of(settings).agent.project_id
    except TypeError:
        # Источник без настроек (заглушка контекста) — имя по пути.
        return None


def project_key_for(root: str, settings: Any = None) -> ProjectKey:
    """Проект каталога ``root``: id — ``agent.project_id`` из ``settings``,
    иначе slug пути (у того же репозитория на другом хосте он другой)."""
    from gc_core.config_modules.env_vars import project_slug

    configured = _configured_project_id(settings)
    return ProjectKey(id=configured or project_slug(root), root=root)


def project_key_of(source: Any) -> ProjectKey | None:
    """Проект контекста: каталог, чей ``.gc/`` держит его память."""
    gc_dir = getattr(source, "project_gc_dir", None)
    if gc_dir is None:
        return None
    root = os.path.dirname(os.path.abspath(str(gc_dir)))
    return project_key_for(root, source)


def decode_text(data: bytes) -> str:
    """Текст ресурса — как ``Path.read_text(encoding="utf-8")``: переводы
    строк ``\\r\\n`` и ``\\r`` приводятся к ``\\n``."""
    text = data.decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def require_project(
    kind: ResourceKind, name: str, project: ProjectKey | None
) -> None:
    """Ресурс проекта без проекта — ошибка вызывающего (ядра), а не «ресурса
    нет»: молча пустой AGENTS.md проекта спрятал бы потерянный проект."""
    if project is None and needs_project(kind, name):
        raise ValueError(f"{kind}:{name} — ресурс проекта: нужен project")


def _absent(label: str) -> FileNotFoundError:
    return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), label)


async def _aread(
    store: AgentStore,
    kind: ResourceKind,
    name: str,
    project: ProjectKey | None,
) -> str:
    data = await store.read(kind, name, project=project)
    if data is None:
        raise _absent(store.label(kind, name, project=project))
    return decode_text(data)


async def aread_text(
    store: AgentStore,
    kind: ResourceKind,
    name: str,
    *,
    project: ProjectKey | None = None,
) -> str:
    """Текст ресурса либо ``ValueError`` с его именем — как
    ``read_user_text``."""
    try:
        return await _aread(store, kind, name, project)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(
            unreadable_reason(store.label(kind, name, project=project), exc)
        ) from exc


async def aread_or_none(
    store: AgentStore,
    kind: ResourceKind,
    name: str,
    *,
    project: ProjectKey | None = None,
) -> str | None:
    """Текст ресурса либо ``None`` — как ``read_user_text_or_none``."""
    try:
        return await _aread(store, kind, name, project)
    except (OSError, UnicodeDecodeError):
        return None


async def aread_required(
    store: AgentStore,
    kind: ResourceKind,
    name: str,
    *,
    missing: str,
    project: ProjectKey | None = None,
) -> str:
    """Обязательный ресурс: нет — ``FileNotFoundError(missing)``, не
    читается — ``ValueError`` с его именем."""
    try:
        return await _aread(store, kind, name, project)
    except FileNotFoundError:
        raise FileNotFoundError(missing) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(
            unreadable_reason(store.label(kind, name, project=project), exc)
        ) from exc
