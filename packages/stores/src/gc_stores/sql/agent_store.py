"""``SqlAgentStore`` — хранилище агента на SQL-базе (общий движок).

Ресурсы — строки ``gc_agent_resources`` по ключу ``(agent_id, project_id,
kind, path)``: ``project_id`` пуст у ресурсов агента (промпты, L0, L1,
субагенты, скиллы ``system``/``user``) и равен id проекта у ресурсов проекта
(L2, L3, скиллы ``project``). Каталоги (скиллы, субагенты) выводятся из
путей. Память проекта — ``gc_project_memory`` по ``(agent_id,
project_id)``: запись с проверкой версии под блокировкой строки.

Контракт ядра ресурсы только читает; записывает их ``gc-stores sync``
через ``ResourceWriter`` (``put_resource``/``delete_resource``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, TypeVar

from gc_core.agent_store import (
    AgentStore,
    MemoryConflict,
    ProjectKey,
    ProjectMemory,
    ResourceInfo,
    ResourceKind,
    memory_version,
    needs_project,
    require_project,
)

from gc_stores import migrations as schema
from gc_stores.admin import StoredResource
from gc_stores.defaults import StaleDefaults
from gc_stores.sql.database import (
    Conn,
    Database,
    guarded,
    in_transaction,
    read_on,
)
from gc_stores.sql.dialect import Dialect

T = TypeVar("T")
_RESOURCE_COLUMNS = (
    "agent_id",
    "project_id",
    "kind",
    "path",
    "data",
    "version",
    "source",
    "updated_at",
)
_RESOURCE_KEY = ("agent_id", "project_id", "kind", "path")
_MEMORY_COLUMNS = ("agent_id", "project_id", "text", "version", "updated_at")


def _normalized(text: str) -> str:
    """Память читается с приведением переводов строк — версия записи та
    же, что у следующего чтения."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class _Queries:
    def __init__(self, d: Dialect) -> None:
        c = d.ident
        res, mem = d.table("gc_agent_resources"), d.table("gc_project_memory")
        p = d.params()
        key = (
            f"{c('agent_id')} = {p()} AND {c('project_id')} = {p()} "
            f"AND {c('kind')} = {p()}"
        )
        self.read = (
            f"SELECT {c('data')}, {c('source')} FROM {res} "
            f"WHERE {key} AND {c('path')} = {p()}"
        )
        p = d.params()
        key = (
            f"{c('agent_id')} = {p()} AND {c('project_id')} = {p()} "
            f"AND {c('kind')} = {p()}"
        )
        self.list = (
            f"SELECT {c('path')} FROM {res} WHERE {key} AND "
            f"({c('path')} = {p()} OR substr({c('path')}, 1, {p()}) = {p()})"
        )
        self.put = d.upsert(res, _RESOURCE_COLUMNS, _RESOURCE_KEY)
        self.reserve_resource = d.insert_ignore(res, _RESOURCE_COLUMNS)
        self.lock_resource = self.read + d.lock_suffix
        p = d.params()
        self.delete = (
            f"DELETE FROM {res} WHERE {c('agent_id')} = {p()} "
            f"AND {c('project_id')} = {p()} AND {c('kind')} = {p()} "
            f"AND {c('path')} = {p()}"
        )
        p = d.params()
        self.all = (
            f"SELECT {c('kind')}, {c('path')}, {c('project_id')}, "
            f"{c('version')}, {c('source')} FROM {res} "
            f"WHERE {c('agent_id')} = {p()} "
            f"AND ({c('project_id')} = '' OR {c('project_id')} = {p()}) "
            f"ORDER BY {c('project_id')}, {c('kind')}, {c('path')}"
        )
        p = d.params()
        by_project = f"{c('agent_id')} = {p()} AND {c('project_id')} = {p()}"
        self.memory = (
            f"SELECT {c('text')}, {c('version')} FROM {mem} WHERE {by_project}"
        )
        self.lock_memory = self.memory + d.lock_suffix
        self.reserve_memory = d.insert_ignore(mem, _MEMORY_COLUMNS)
        p = d.params()
        self.write_memory = (
            f"UPDATE {mem} SET {c('text')} = {p()}, {c('version')} = {p()}, "
            f"{c('updated_at')} = {p()} WHERE {c('agent_id')} = {p()} "
            f"AND {c('project_id')} = {p()}"
        )


class SqlAgentStore(AgentStore):
    """Ресурсы и память агента ``agent_id`` в SQL-базе."""

    def __init__(
        self,
        database: Database,
        dialect: Dialect,
        *,
        agent_id: str,
        backend: str,
        migrations: list[schema.Migration] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not agent_id:
            raise ValueError("нужен agent_id: ресурсы агента в базе по нему")
        dialect.check_resource(agent_id=agent_id)
        self.agent_id = agent_id
        self._db = database
        self._d = dialect
        self._q = _Queries(dialect)
        self._backend = backend
        self._migrations = migrations or []
        self._checked = False
        self._clock = clock
        self._stale = StaleDefaults(f"{backend}:{agent_id}")

    # --- подключение и схема ------------------------------------------------

    async def _ready(self) -> None:
        if self._checked or not self._migrations:
            return
        await schema.check(self._db, self._d, self._migrations)
        self._checked = True

    async def ensure_schema(self) -> None:
        await schema.ensure(self._db, self._d, self._migrations)
        self._checked = True

    async def check_schema(self) -> None:
        """Сверить версию схемы базы с кодом; расхождение —
        ``SchemaMismatch`` с тем, что сделать."""
        self._checked = False
        await self._ready()

    async def _read(self, work: Callable[[Conn], Awaitable[T]]) -> T:
        await guarded(self._db, self._ready())
        return await read_on(self._db, work)

    async def _write(self, work: Callable[[Conn], Awaitable[T]]) -> T:
        await guarded(self._db, self._ready())
        return await in_transaction(self._db, work)

    async def _now(self, conn: Conn) -> Any:
        moment = (
            self._clock()
            if self._clock is not None
            else await self._d.now(conn)
        )
        return self._d.time_in(moment)

    async def close(self) -> None:
        await self._db.close()

    async def __aenter__(self) -> SqlAgentStore:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # --- ресурсы ------------------------------------------------------------

    def _project_id(
        self, kind: ResourceKind, name: str, project: ProjectKey | None
    ) -> str:
        require_project(kind, name, project)
        return project.id if needs_project(kind, name) else ""

    def label(
        self,
        kind: ResourceKind,
        name: str,
        *,
        project: ProjectKey | None = None,
    ) -> str:
        where = (
            f"{project.id}/" if project and needs_project(kind, name) else ""
        )
        label = f"{self._backend}:{self.agent_id}/{where}{kind}/{name}"
        if kind == "prompt":
            label += " (нет в хранилище — gc-stores sync --with-defaults)"
        return label

    def memory_label(self, project: ProjectKey) -> str:
        return f"{self._backend}:{self.agent_id}/memory/{project.id}"

    async def read(
        self,
        kind: ResourceKind,
        name: str,
        *,
        project: ProjectKey | None = None,
    ) -> bytes | None:
        project_id = self._project_id(kind, name, project)
        path = name.strip("/")

        async def work(conn: Conn) -> Mapping[str, Any] | None:
            return await conn.fetchrow(
                self._q.read, self.agent_id, project_id, kind, path
            )

        found = await self._read(work)
        if found is None:
            return None
        self._stale.check(found["source"])
        return bytes(found["data"])

    async def list(
        self,
        kind: ResourceKind,
        prefix: str = "",
        *,
        project: ProjectKey | None = None,
    ) -> list[ResourceInfo]:
        project_id = self._project_id(kind, prefix, project)
        base = prefix.strip("/")
        head = f"{base}/" if base else ""

        async def work(conn: Conn) -> list[Mapping[str, Any]]:
            return await conn.fetch(
                self._q.list,
                self.agent_id,
                project_id,
                kind,
                base,
                len(head),
                head,
            )

        paths = [row["path"] for row in await self._read(work)]
        if base in paths:
            raise NotADirectoryError(self.label(kind, prefix, project=project))
        entries: dict[str, str] = {}
        for path in paths:
            name, sep, _rest = path[len(head) :].partition("/")
            entries[name] = "dir" if sep else "file"
        if not entries:
            raise FileNotFoundError(self.label(kind, prefix, project=project))
        return [ResourceInfo(n, t) for n, t in sorted(entries.items())]

    # --- память проекта -----------------------------------------------------

    async def read_memory(self, project: ProjectKey) -> ProjectMemory | None:
        async def work(conn: Conn) -> Mapping[str, Any] | None:
            return await conn.fetchrow(
                self._q.memory, self.agent_id, project.id
            )

        found = await self._read(work)
        if found is None:
            return None
        return ProjectMemory(found["text"], found["version"])

    async def write_memory(
        self,
        project: ProjectKey,
        text: str,
        *,
        expected_version: str | None,
    ) -> ProjectMemory:
        stored = _normalized(text)
        version = memory_version(stored)
        self._d.check_resource(project_id=project.id)

        async def work(conn: Conn) -> ProjectMemory:
            now = await self._now(conn)
            # Строку — сначала (пустой резерв): блокировка «строки, которой
            # нет» не держит параллельную вставку, и две первые записи
            # разошлись бы молча. Резерв откатывается вместе с отказом.
            reserved = await conn.execute(
                self._q.reserve_memory,
                self.agent_id,
                project.id,
                "",
                None,
                now,
            )
            found = await conn.fetchrow(
                self._q.lock_memory, self.agent_id, project.id
            )
            current = (
                None
                if reserved
                else ProjectMemory(found["text"], found["version"])
            )
            current_version = current.version if current else None
            if current_version != expected_version:
                raise MemoryConflict(current)
            await conn.execute(
                self._q.write_memory,
                stored,
                version,
                now,
                self.agent_id,
                project.id,
            )
            return ProjectMemory(stored, version)

        return await self._write(work)

    # --- запись ресурсов (ResourceWriter) ------------------------------------

    async def put_resource(
        self,
        kind: ResourceKind,
        name: str,
        data: bytes,
        *,
        project: str | None = None,
        source: str | None = None,
        preserve_manual: bool = False,
    ) -> bool:
        path = name.strip("/")
        project_id = project or ""
        self._d.check_resource(project_id=project_id, path=path)
        digest = hashlib.sha256(data).hexdigest()

        async def work(conn: Conn) -> bool:
            values = (
                self.agent_id,
                project_id,
                kind,
                path,
                data,
                digest,
                source,
                await self._now(conn),
            )
            if preserve_manual:
                # Резервируем отсутствующий ключ, чтобы ручная первая
                # запись не могла вклиниться между SELECT и UPSERT.
                await conn.execute(self._q.reserve_resource, *values)
                current = await conn.fetchrow(
                    self._q.lock_resource, *values[:4]
                )
                if current["source"] is None:
                    return False
            await conn.execute(self._q.put, *values)
            return True

        return await self._write(work)

    async def delete_resource(
        self, kind: ResourceKind, name: str, *, project: str | None = None
    ) -> None:
        async def work(conn: Conn) -> None:
            await conn.execute(
                self._q.delete,
                self.agent_id,
                project or "",
                kind,
                name.strip("/"),
            )

        await self._write(work)

    async def list_resources(
        self, *, project: str | None = None
    ) -> list[StoredResource]:
        async def work(conn: Conn) -> list[Mapping[str, Any]]:
            return await conn.fetch(self._q.all, self.agent_id, project or "")

        return [
            StoredResource(
                kind=row["kind"],
                name=row["path"],
                project=row["project_id"] or None,
                version=row["version"],
                source=row["source"],
            )
            for row in await self._read(work)
        ]


__all__ = ["SqlAgentStore"]
