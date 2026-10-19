"""Запись ресурсов агента в хранилище — для администратора, не для ядра.

Контракт ``AgentStore`` ядро только читает (кроме памяти проекта): промпты,
AGENTS.md, скиллы и субагенты в хранилище в базе кладёт ``gc-stores sync``
через этот интерфейс. Его реализуют хранилища агента ``gc-stores``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gc_core.agent_store import ResourceKind


@dataclass(frozen=True)
class StoredResource:
    """Ресурс в хранилище: ``project`` — id проекта (``None`` — ресурс
    агента); ``version`` — sha256 содержимого; ``source`` — ``None`` у
    загруженного человеком, ``bundled@<версия gc-core>`` у умолчания пакета."""

    kind: ResourceKind
    name: str
    project: str | None
    version: str
    source: str | None


class ResourceWriter(Protocol):
    async def put_resource(
        self,
        kind: ResourceKind,
        name: str,
        data: bytes,
        *,
        project: str | None = None,
        source: str | None = None,
    ) -> None:
        """Записать (или заменить) ресурс."""

    async def delete_resource(
        self, kind: ResourceKind, name: str, *, project: str | None = None
    ) -> None:
        """Удалить ресурс; нет — ничего."""

    async def list_resources(
        self, *, project: str | None = None
    ) -> list[StoredResource]:
        """Ресурсы агента и, если задан ``project``, ресурсы проекта."""
