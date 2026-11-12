"""Клиент прокси одного треда — ``ThreadStore`` для песочницы swarm.

Учётных данных базы у песочницы нет: она ходит в хранилище тредов сервера
через unix-сокет своей задачи, с токеном задачи. Чужой тред и операции
не хода (список, алиасы, удаление, обслуживание) сервер отвергает —
``PermissionError``. Сокет недоступен — ``StoreUnavailable`` (сессия
повторит запись с тем же ``op_id``)."""

from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
from typing import Any

from gc_core.threads.errors import StoreUnavailable
from gc_core.threads.models import (
    BlobRef,
    Lease,
    LeaseRequest,
    Page,
    Receipt,
    SideChange,
    StreamEntry,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
    ThreadRecord,
)
from gc_core.threads.store import ThreadStore

from gc_stores.remote import protocol
from gc_stores.remote.protocol import wire


class RemoteThreadStore(ThreadStore):
    """Хранилище тредов за сокетом ``socket`` с токеном задачи ``token``."""

    def __init__(self, socket: str, token: str) -> None:
        self.socket = socket
        self._token = token

    async def _call(self, op: str, key: ThreadKey | None, **args: Any) -> Any:
        request = {
            "token": self._token,
            "op": op,
            "key": wire(key) if key is not None else None,
            "args": args,
        }
        try:
            reader, writer = await asyncio.open_unix_connection(
                self.socket, limit=protocol.FRAME_LIMIT
            )
        except OSError as exc:
            raise StoreUnavailable.from_error(exc) from exc
        try:
            writer.write(protocol.encode(request))
            await writer.drain()
            line = await reader.readline()
        except OSError as exc:
            raise StoreUnavailable.from_error(exc) from exc
        finally:
            writer.close()
        if not line:
            raise StoreUnavailable("прокси треда закрыл соединение без ответа")
        reply = protocol.decode(line)
        if "error" in reply:
            raise protocol.error_of(reply["error"])
        return reply.get("ok")

    async def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        found = await self._call("get_thread", key)
        return None if found is None else ThreadRecord.model_validate(found)

    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]:
        found = await self._call(
            "list_threads",
            None,
            query=wire(query),
        )
        return Page[ThreadRecord].model_validate(found)

    async def read_stream(
        self,
        key: ThreadKey,
        stream: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[StreamEntry]:
        found = await self._call(
            "read_stream", key, stream=stream, after_seq=after_seq, limit=limit
        )
        return [StreamEntry.model_validate(entry) for entry in found]

    async def read_archive(
        self, key: ThreadKey, archive_id: str
    ) -> list[StreamEntry]:
        found = await self._call("read_archive", key, archive_id=archive_id)
        return [StreamEntry.model_validate(entry) for entry in found]

    async def resolve_alias(self, alias: str) -> ThreadKey | None:
        found = await self._call("resolve_alias", None, alias=alias)
        return None if found is None else ThreadKey.model_validate(found)

    async def commit(self, key: ThreadKey, change: ThreadChange) -> Receipt:
        found = await self._call(
            "commit",
            key,
            change=wire(change),
        )
        return Receipt.model_validate(found)

    async def append_side(self, key: ThreadKey, change: SideChange) -> Receipt:
        found = await self._call(
            "append_side",
            key,
            change=wire(change),
        )
        return Receipt.model_validate(found)

    async def claim(self, key: ThreadKey, lease: LeaseRequest) -> ThreadRecord:
        found = await self._call(
            "claim",
            key,
            request=wire(lease),
        )
        return ThreadRecord.model_validate(found)

    async def release(self, key: ThreadKey, lease: Lease) -> None:
        await self._call(
            "release",
            key,
            lease=wire(lease),
        )

    async def delete(self, key: ThreadKey, *, op_id: str) -> None:
        await self._call("delete", key, op_id=op_id)

    async def put_blob(
        self, key: ThreadKey, data: bytes, *, mime: str
    ) -> BlobRef:
        encoded = base64.b64encode(data).decode("ascii")
        found = await self._call("put_blob", key, data=encoded, mime=mime)
        return BlobRef.model_validate(found)

    async def get_blob(self, ref: BlobRef) -> bytes:
        found = await self._call(
            "get_blob",
            ref.key,
            ref=wire(ref),
        )
        return base64.b64decode(found)

    async def prune_receipts(self, older_than: timedelta) -> int:
        return await self._call(
            "prune_receipts", None, seconds=older_than.total_seconds()
        )
