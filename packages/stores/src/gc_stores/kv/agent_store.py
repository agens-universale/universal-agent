"""``KvAgentStore`` — хранилище агента на KV (Redis, S3).

Ресурс — сырые байты по ключу ``res/<агент>/<проект или _>/<вид>/<путь>``
(агент и проект — hex, путь — как есть: каталоги скиллов выводятся
списком по префиксу). Версии и метки источников ресурсов агента — в его
манифесте ``res/<агент>/manifest``: его правит запись ресурсов (условной
записью, повтор при гонке). Память проекта — ``mem/<агент>/<проект>``,
запись с проверкой версии — условной записью ключа.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

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

from gc_stores.admin import StoredResource
from gc_stores.defaults import StaleDefaults
from gc_stores.kv import keys
from gc_stores.kv.base import KV, ensure_format, guarded, read_format

_AGENT_LEVEL = "_"
_ATTEMPTS = 16


def _scope(project: str | None) -> str:
    return keys.enc(project) if project else _AGENT_LEVEL


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


class KvAgentStore(AgentStore):
    """Ресурсы и память агента ``agent_id`` в KV ``kv``; ``backend`` —
    имя хранилища для текстов отказа (``redis``, ``s3``)."""

    def __init__(self, kv: KV, *, agent_id: str, backend: str) -> None:
        if not agent_id:
            raise ValueError(
                "нужен agent_id: ресурсы агента в хранилище по нему"
            )
        self.agent_id = agent_id
        self._kv = guarded(kv)
        self._backend = backend
        self._agent = f"res/{keys.enc(agent_id)}/"
        self._ready_done = False
        self._stale = StaleDefaults(f"{backend}:{agent_id}")
        self._stale_checked = False

    # --- подключение -------------------------------------------------------

    async def _ready(self) -> None:
        if self._ready_done:
            return
        await self._kv.check()
        await ensure_format(self._kv)
        self._ready_done = True

    async def ensure_schema(self) -> None:
        """Записать номер формата ключей, если его нет; иной — отказ."""
        self._ready_done = False
        await self._ready()

    async def check_schema(self) -> None:
        await self._kv.check()
        await read_format(self._kv, required=True)

    async def close(self) -> None:
        await self._kv.close()

    async def __aenter__(self) -> KvAgentStore:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # --- ключи ------------------------------------------------------------

    def _resource(self, scope: str, kind: str, path: str) -> str:
        return f"{self._agent}{scope}/{kind}/{path}"

    def _scope_of(
        self, kind: ResourceKind, name: str, project: ProjectKey | None
    ) -> str:
        require_project(kind, name, project)
        return _scope(project.id if needs_project(kind, name) else None)

    def _memory(self, project: ProjectKey) -> str:
        return f"mem/{keys.enc(self.agent_id)}/{keys.enc(project.id)}"

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

    # --- ресурсы ----------------------------------------------------------

    async def _check_stale(self) -> None:
        """Один раз: умолчания пакета другой версии — предупреждение."""
        if self._stale_checked:
            return
        self._stale_checked = True
        manifest, _version = await self._manifest()
        for entry in manifest.values():
            self._stale.check(entry.get("source"))

    async def read(
        self,
        kind: ResourceKind,
        name: str,
        *,
        project: ProjectKey | None = None,
    ) -> bytes | None:
        scope = self._scope_of(kind, name, project)
        await self._ready()
        found = await self._kv.get(
            self._resource(scope, kind, name.strip("/"))
        )
        if found is None:
            return None
        await self._check_stale()
        return found.data

    async def list(
        self,
        kind: ResourceKind,
        prefix: str = "",
        *,
        project: ProjectKey | None = None,
    ) -> list[ResourceInfo]:
        scope = self._scope_of(kind, prefix, project)
        await self._ready()
        base = prefix.strip("/")
        root = self._resource(scope, kind, "")
        if base and await self._kv.get(root + base) is not None:
            raise NotADirectoryError(self.label(kind, prefix, project=project))
        head = root + (f"{base}/" if base else "")
        entries: dict[str, str] = {}
        start = ""
        while True:
            batch = await self._kv.scan(head, start_after=start, limit=1000)
            for name in batch:
                child, sep, _rest = name[len(head) :].partition("/")
                entries[child] = "dir" if sep else "file"
            if len(batch) < 1000:
                break
            start = batch[-1]
        if not entries:
            raise FileNotFoundError(self.label(kind, prefix, project=project))
        return [ResourceInfo(n, t) for n, t in sorted(entries.items())]

    # --- память проекта ---------------------------------------------------

    async def read_memory(self, project: ProjectKey) -> ProjectMemory | None:
        await self._ready()
        found = await self._kv.get(self._memory(project))
        if found is None:
            return None
        record = json.loads(found.data)
        return ProjectMemory(record["text"], record["version"])

    async def write_memory(
        self,
        project: ProjectKey,
        text: str,
        *,
        expected_version: str | None,
    ) -> ProjectMemory:
        await self._ready()
        stored = _normalized(text)
        version = memory_version(stored)
        record = json.dumps(
            {"text": stored, "version": version}, ensure_ascii=False
        ).encode("utf-8")
        name = self._memory(project)
        found = await self._kv.get(name)
        current = None
        if found is not None:
            data = json.loads(found.data)
            current = ProjectMemory(data["text"], data["version"])
        if (current.version if current else None) != expected_version:
            raise MemoryConflict(current)
        written = await self._kv.put(
            name, record, if_version=None if found is None else found.version
        )
        if written is None:
            # Успели раньше: свежая память — для повторной правки.
            raise MemoryConflict(await self.read_memory(project))
        return ProjectMemory(stored, version)

    # --- запись ресурсов (ResourceWriter) ----------------------------------

    async def _manifest(self) -> tuple[dict[str, Any], str | None]:
        found = await self._kv.get(self._agent + "manifest")
        if found is None:
            return {}, None
        return json.loads(found.data), found.version

    async def _update_manifest(self, entry: str, value: dict | None) -> None:
        for _ in range(_ATTEMPTS):
            manifest, version = await self._manifest()
            if value is None:
                manifest.pop(entry, None)
            else:
                manifest[entry] = value
            data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            if await self._kv.put(
                self._agent + "manifest", data, if_version=version
            ):
                return
        raise RuntimeError("манифест ресурсов агента правят наперегонки")

    async def put_resource(
        self,
        kind: ResourceKind,
        name: str,
        data: bytes,
        *,
        project: str | None = None,
        source: str | None = None,
    ) -> None:
        await self._ready()
        path = name.strip("/")
        scope = _scope(project)
        await self._kv.put_any(self._resource(scope, kind, path), data)
        await self._update_manifest(
            f"{scope}|{kind}|{path}",
            {"version": hashlib.sha256(data).hexdigest(), "source": source},
        )

    async def delete_resource(
        self, kind: ResourceKind, name: str, *, project: str | None = None
    ) -> None:
        await self._ready()
        path = name.strip("/")
        scope = _scope(project)
        await self._kv.delete(self._resource(scope, kind, path))
        await self._update_manifest(f"{scope}|{kind}|{path}", None)

    async def list_resources(
        self, *, project: str | None = None
    ) -> list[StoredResource]:
        await self._ready()
        manifest, _version = await self._manifest()
        wanted = {_AGENT_LEVEL, _scope(project)}
        found = []
        for entry, value in sorted(manifest.items()):
            scope, kind, path = entry.split("|", 2)
            if scope not in wanted:
                continue
            found.append(
                StoredResource(
                    kind=kind,
                    name=path,
                    project=None if scope == _AGENT_LEVEL else keys.dec(scope),
                    version=value["version"],
                    source=value["source"],
                )
            )
        return found


__all__ = ["KvAgentStore"]
