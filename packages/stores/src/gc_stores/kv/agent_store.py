"""``KvAgentStore`` — хранилище агента на KV (Redis, S3).

Ресурс по ключу ``res/<агент>/<проект или _>/<вид>/<путь>`` содержит
байты и источник в одном условно обновляемом объекте. Манифест кеширует
сведения; список сверяет его с объектами. Старые сырые ресурсы читаются
с источником из манифеста. Память проекта — ``mem/<агент>/<проект>``,
запись с проверкой версии — условной записью ключа.
"""

from __future__ import annotations

import base64
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
_RESOURCE_V2 = b"GC_RESOURCE_V2\x00"


def _resource_document(
    data: bytes, legacy: dict | None = None
) -> tuple[bytes, dict]:
    if data.startswith(_RESOURCE_V2):
        doc = json.loads(data[len(_RESOURCE_V2) :])
        content = base64.b64decode(doc["data"])
        return content, {
            "version": hashlib.sha256(content).hexdigest(),
            "source": doc["source"],
        }
    return data, {
        "version": hashlib.sha256(data).hexdigest(),
        "source": (legacy or {}).get("source", HEALED_SOURCE),
    }


def _scope(project: str | None) -> str:
    return keys.enc(project) if project else _AGENT_LEVEL


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


#: Источник ресурса, найденного без записи манифеста: чей он — неизвестно.
#: Не ``None`` (ресурс человека): его умолчания пакета не обновили бы никогда.
HEALED_SOURCE = "unknown"


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
        return _resource_document(found.data)[0]

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
        await self._patch_manifest({entry: value})

    async def _patch_manifest(
        self,
        patch: dict[str, dict | None],
        *,
        only_if_absent: bool = False,
    ) -> None:
        """Правки манифеста одной условной записью (повтор при гонке):
        ``None`` — убрать запись. ``only_if_absent`` — не трогать записи,
        которые успел сделать другой писатель (самопочинка)."""
        for _ in range(_ATTEMPTS):
            manifest, version = await self._manifest()
            for entry, value in patch.items():
                if only_if_absent and (entry in manifest) == (
                    value is not None
                ):
                    continue
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

    async def _stored(self, scope: str) -> dict[str, str]:
        """Записи манифеста по ресурсам, что лежат в KV: запись → ключ."""
        head = f"{self._agent}{scope}/"
        found: dict[str, str] = {}
        start = ""
        while True:
            batch = await self._kv.scan(head, start_after=start, limit=1000)
            for key in batch:
                kind, _sep, path = key[len(head) :].partition("/")
                found[f"{scope}|{kind}|{path}"] = key
            if len(batch) < 1000:
                return found
            start = batch[-1]

    async def _heal(
        self, manifest: dict[str, Any], scopes: set[str]
    ) -> dict[str, Any]:
        """Список по объектам ресурсов; манифест — лишь кеш.

        Только старый сырой объект без манифеста получает HEALED_SOURCE.
        Новый объект сохраняет источник даже при обрыве обновления кеша.
        """
        stored: dict[str, str] = {}
        for scope in scopes:
            stored.update(await self._stored(scope))
        patch: dict[str, dict | None] = {}
        authoritative = dict(manifest)
        for entry, key in stored.items():
            found = await self._kv.get(key)
            if found is not None:
                _content, info = _resource_document(
                    found.data, manifest.get(entry)
                )
                authoritative[entry] = info
                if manifest.get(entry) != info:
                    patch[entry] = info
        for entry in manifest:
            if entry.split("|", 1)[0] in scopes and entry not in stored:
                patch[entry] = None
                authoritative.pop(entry, None)
        if not patch:
            return manifest
        await self._patch_manifest(patch, only_if_absent=True)
        # Манифест — кеш каталога. Байты и источник одной версии берутся
        # из единого объекта, даже если другой писатель обновляет кеш.
        return authoritative

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
        await self._ready()
        path = name.strip("/")
        scope = _scope(project)
        key = self._resource(scope, kind, path)
        entry = f"{scope}|{kind}|{path}"
        document = (
            _RESOURCE_V2
            + json.dumps(
                {
                    "data": base64.b64encode(data).decode("ascii"),
                    "source": source,
                }
            ).encode()
        )
        for _ in range(_ATTEMPTS):
            current = await self._kv.get(key)
            if preserve_manual and current is not None:
                manifest, _version = await self._manifest()
                _body, info = _resource_document(
                    current.data, manifest.get(entry)
                )
                if info["source"] is None:
                    return False
            if await self._kv.put(
                key, document, if_version=current.version if current else None
            ):
                break
        else:
            raise RuntimeError("ресурс агента правят наперегонки")
        await self._update_manifest(
            entry,
            {"version": hashlib.sha256(data).hexdigest(), "source": source},
        )
        return True

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
        wanted = {_AGENT_LEVEL, _scope(project)}
        manifest, _version = await self._manifest()
        manifest = await self._heal(manifest, wanted)
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
