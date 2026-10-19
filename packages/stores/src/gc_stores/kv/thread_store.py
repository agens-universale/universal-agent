"""``KvThreadStore`` — хранилище тредов v2 на KV (Redis, S3).

Транзакций у KV нет, есть условная запись одного ключа. Инвариант движка:
**точка фиксации — условная запись заголовка треда** (``kv/head.py``). Всё,
что пишется до неё, лежит под уникальными ключами и невидимо через контракт,
пока заголовок на это не сослался:

* записи потока — неизменяемые куски (``t/…/c/<id>``), заголовок держит
  их список;
* алиас — резерв ``alias/<alias>`` с владельцем; действителен, только если
  заголовок владельца его перечисляет;
* индекс списка — ключ ``idx/<перевёрнутое время>/…``; читатель сверяет его
  с заголовком.

Отклонённая или прерванная запись следов не оставляет; повтор с тем же
``op_id`` её доводит. Квитанция последней записи живёт в заголовке, а
следующая запись выносит её в объект ``r/…`` — объекта квитанции без
применённой записи не бывает. Сироты прерванных записей убирает ``sweep``.

Конкурент успел записать заголовок первым — шаг повторяется на свежем
заголовке (ревизия решает, кто проиграл).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from gc_core.threads.errors import OperationMismatch, ThreadConflict
from gc_core.threads.models import (
    ArchiveInfo,
    BlobRef,
    Compaction,
    JsonObject,
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
    change_digest,
    check_stream_name,
)
from gc_core.threads.rules import (
    check_side_writer,
    check_writer,
    granted_lease,
    instant,
    same_lease,
)
from gc_core.threads.store import ThreadStore

from gc_stores.common import EXTENSIONS, apply_sets, apply_side
from gc_stores.kv import keys
from gc_stores.kv.base import KV, ensure_format, read_format
from gc_stores.kv.head import Chunk, Head
from gc_stores.rows import HISTORY, dumps, id_key, message_key, stamp

_log = logging.getLogger("gc_stores")

#: Сколько раз шаг записи повторяется, если заголовок успели переписать.
ATTEMPTS = 16
#: Свежий резерв алиаса (заголовок владельца ещё не записан) держит алиас:
#: два треда одновременно не получат его оба. Старше — сирота, перехват.
ALIAS_HOLD = timedelta(minutes=1)
#: Потоку с числом кусков больше ``MERGE_AT`` первые ``MERGE_COUNT`` кусков
#: склеиваются в один: чтение не превращается в тысячи запросов.
MERGE_AT = 64
MERGE_COUNT = 32
_SCAN_PAGE = 1000
_READ_BATCH = 8


@dataclass
class _Done:
    result: Any


@dataclass
class _Write:
    head: Head
    result: Any
    after: list[str] = field(default_factory=list)


def _live(head: Head | None) -> Head | None:
    return None if head is None or head.deleted else head


def _chunk_bytes(now: datetime, entries: list[list[Any]]) -> bytes:
    document = {"at": now.isoformat(), "entries": entries}
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


class _ChunkLost(LookupError):
    """Кусок из прочитанного заголовка уже убран (сжатие истории) —
    перечитать заголовок."""


class KvThreadStore(ThreadStore):
    """Треды в KV ``kv``; ``clock`` — часы хранилища (аренда, квитанции):
    у KV своих часов нет — часы машины, они должны быть синхронны (NTP)."""

    def __init__(
        self,
        kv: KV,
        *,
        clock: Callable[[], datetime] | None = None,
        attempts: int = ATTEMPTS,
    ) -> None:
        self._kv = kv
        self._clock = clock or (lambda: datetime.now(UTC))
        self._attempts = attempts
        self._ready_done = False

    # --- подключение и формат ----------------------------------------------

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

    async def schema_version(self) -> int:
        return await read_format(self._kv, required=False)

    def protected_paths(self) -> list[str]:
        """Данных на диске машины нет. Объявление обязательно для воркера
        Temporal."""
        return []

    async def close(self) -> None:
        await self._kv.close()

    async def __aenter__(self) -> KvThreadStore:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # --- заголовок и цикл записи -------------------------------------------

    async def _head(self, key: ThreadKey) -> tuple[Head | None, str | None]:
        found = await self._kv.get(keys.head_key(key))
        if found is None:
            return None, None
        return Head.loads(found.data), found.version

    async def _mutate(
        self, key: ThreadKey, step: Callable[[Head | None], Awaitable[Any]]
    ) -> Any:
        await self._ready()
        for _ in range(self._attempts):
            head, version = await self._head(key)
            outcome = await step(head)
            if isinstance(outcome, _Done):
                return outcome.result
            written = await self._kv.put(
                keys.head_key(key), outcome.head.dumps(), if_version=version
            )
            if written is not None:
                await self._cleanup(outcome.after)
                return outcome.result
        raise ThreadConflict(
            "заголовок треда менялся быстрее записи: повторите запись"
        )

    async def _cleanup(self, stale: list[str]) -> None:
        """После фиксации — по возможности: что не убралось, уберёт
        ``sweep`` (на это не ссылается ни один заголовок)."""
        for name in stale:
            try:
                await self._kv.delete(name)
            except Exception:  # уборка не отменяет записанное
                _log.warning("kv: не убран %s", name, exc_info=True)

    # --- квитанции -----------------------------------------------------------

    async def _replayed(
        self, key: ThreadKey, head: Head | None, op_id: str, digest: str
    ) -> Receipt | None:
        for item in head.pending if head is not None else ():
            if item["op_id"] == op_id:
                return _receipt_of(item, op_id, digest)
        found = await self._kv.get(keys.receipt_key(key, op_id))
        if found is None:
            return None
        return _receipt_of(json.loads(found.data), op_id, digest)

    async def _settle(
        self,
        key: ThreadKey,
        old: Head | None,
        new: Head,
        receipt_item: dict[str, Any],
        after: list[str],
    ) -> None:
        """Квитанции прошлых записей — в свои объекты (те записи уже
        зафиксированы), текущая — в заголовок; ключ индекса — по времени."""
        for item in old.pending if old is not None else ():
            await self._kv.put_any(
                keys.receipt_key(key, item["op_id"]),
                json.dumps(item, ensure_ascii=False).encode("utf-8"),
            )
        new.pending = [receipt_item]
        await self._reindex(key, new, after)

    async def _reindex(
        self, key: ThreadKey, head: Head, after: list[str]
    ) -> None:
        target = keys.index_key(head.row.updated_at, key)
        if head.index == target:
            return
        await self._kv.put_any(target, b"")
        if head.index is not None:
            after.append(head.index)
        head.index = target

    # --- алиасы --------------------------------------------------------------

    async def _listed(self, owner: ThreadKey, alias: str) -> bool:
        head = _live((await self._head(owner))[0])
        return head is not None and alias in head.aliases

    async def _reserve(
        self, key: ThreadKey, alias: str, now: datetime
    ) -> None:
        record = json.dumps(
            {
                "system": key.system,
                "thread": key.thread,
                "at": now.isoformat(),
            }
        ).encode("utf-8")
        name = keys.alias_key(alias)
        for _ in range(self._attempts):
            found = await self._kv.get(name)
            if found is not None:
                owner, at = _reservation(found.data)
                if owner == key:
                    return
                held = now - at < ALIAS_HOLD
                if held or await self._listed(owner, alias):
                    raise ValueError(
                        f"алиас {alias!r} принадлежит треду "
                        f"{owner.system}/{owner.thread}"
                    )
            version = None if found is None else found.version
            if await self._kv.put(name, record, if_version=version):
                return
        raise ThreadConflict(f"алиас {alias!r} перехватывают наперегонки")

    async def _reserve_aliases(
        self, key: ThreadKey, head: Head, aliases: list[str], now: datetime
    ) -> None:
        for alias in sorted(set(aliases)):
            if alias not in head.aliases:
                await self._reserve(key, alias, now)
                head.aliases.append(alias)

    # --- потоки ----------------------------------------------------------------

    def _chunk_id(self, head: Head) -> str:
        return f"{head.row.revision + 1:012d}-{secrets.token_hex(6)}"

    async def _write_chunk(
        self,
        key: ThreadKey,
        head: Head,
        entries: list[list[Any]],
        now: datetime,
    ) -> Chunk:
        chunk = Chunk(
            self._chunk_id(head), entries[0][0], entries[-1][0], len(entries)
        )
        await self._kv.put_any(
            keys.chunk_key(key, chunk.id), _chunk_bytes(now, entries)
        )
        return chunk

    async def _append(
        self,
        key: ThreadKey,
        head: Head,
        stream: str,
        items: list[JsonObject],
        now: datetime,
    ) -> None:
        if not items:
            return
        seq = head.row.max_seq.get(stream, 0)
        entries = []
        for item in items:
            seq += 1
            entries.append([seq, stamp(now), dumps(item)])
        head.chunks(stream).append(
            await self._write_chunk(key, head, entries, now)
        )
        head.row.max_seq[stream] = seq
        if stream == HISTORY:
            head.row.messages_count += len(items)

    async def _entries(self, key: ThreadKey, chunk: Chunk) -> list[list[Any]]:
        found = await self._kv.get(keys.chunk_key(key, chunk.id))
        if found is None:
            raise _ChunkLost(chunk.id)
        return json.loads(found.data)["entries"]

    def _drop_history(self, key: ThreadKey, head: Head) -> list[str]:
        stale = [
            keys.chunk_key(key, c.id) for c in head.streams.pop(HISTORY, [])
        ]
        head.row.max_seq[HISTORY] = 0
        head.row.messages_count = 0
        return stale

    async def _replace(
        self,
        key: ThreadKey,
        head: Head,
        compaction: Compaction,
        now: datetime,
        after: list[str],
    ) -> str | None:
        archive_id = None
        if compaction.archive:
            archive_id = f"a{len(head.row.archives) + 1}"
            head.archive_chunks[archive_id] = head.streams.pop(HISTORY, [])
            head.row.archives.append(
                ArchiveInfo(
                    id=archive_id,
                    created_at=stamp(now),
                    messages_count=head.row.messages_count,
                )
            )
        after.extend(self._drop_history(key, head))
        await self._append(key, head, HISTORY, compaction.messages, now)
        head.row.history_version += 1
        return archive_id

    async def _discard(
        self,
        key: ThreadKey,
        head: Head,
        ids: list[str],
        now: datetime,
        after: list[str],
    ) -> None:
        if not ids:
            return
        wanted = {id_key(i) for i in ids}
        kept: list[Chunk] = []
        for chunk in head.streams.get(HISTORY, []):
            entries = await self._entries(key, chunk)
            remaining = [
                e
                for e in entries
                if message_key(json.loads(e[2])) not in wanted
            ]
            if len(remaining) == len(entries):
                kept.append(chunk)
                continue
            head.row.messages_count -= len(entries) - len(remaining)
            after.append(keys.chunk_key(key, chunk.id))
            if remaining:
                kept.append(await self._write_chunk(key, head, remaining, now))
        head.streams[HISTORY] = kept

    async def _merge(
        self, key: ThreadKey, head: Head, now: datetime, after: list[str]
    ) -> None:
        """Слишком много кусков в потоке — первые склеить в один."""
        for stream, chunks in head.streams.items():
            if len(chunks) <= MERGE_AT:
                continue
            first = chunks[:MERGE_COUNT]
            loaded = await asyncio.gather(
                *(self._entries(key, c) for c in first)
            )
            entries = [entry for part in loaded for entry in part]
            merged = await self._write_chunk(key, head, entries, now)
            head.streams[stream] = [merged, *chunks[MERGE_COUNT:]]
            after.extend(keys.chunk_key(key, c.id) for c in first)

    async def _apply(
        self,
        key: ThreadKey,
        head: Head,
        change: ThreadChange,
        now: datetime,
        after: list[str],
    ) -> str | None:
        """Изменения ``commit`` в порядке контракта; вернуть id архива."""
        await self._reserve_aliases(key, head, change.add_aliases, now)
        if change.reset_history:
            after.extend(self._drop_history(key, head))
            head.row.history_version += 1
            head.row.generation += 1
            head.row.drop_lease()
        archive_id = None
        if change.replace_history is not None:
            archive_id = await self._replace(
                key, head, change.replace_history, now, after
            )
        await self._discard(key, head, change.discard_message_ids, now, after)
        await self._append(key, head, HISTORY, change.append_messages, now)
        apply_sets(head.row, change)
        await self._merge(key, head, now, after)
        return archive_id

    # --- чтение ----------------------------------------------------------------

    async def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        await self._ready()
        head = _live((await self._head(key))[0])
        return None if head is None else head.row.record()

    async def _collect(
        self,
        key: ThreadKey,
        chunks: list[Chunk],
        after_seq: int,
        limit: int | None,
    ) -> list[StreamEntry]:
        found: list[StreamEntry] = []
        for start in range(0, len(chunks), _READ_BATCH):
            batch = chunks[start : start + _READ_BATCH]
            loaded = await asyncio.gather(
                *(self._entries(key, c) for c in batch)
            )
            for seq, ts, text in (e for part in loaded for e in part):
                if seq <= after_seq:
                    continue
                found.append(
                    StreamEntry(seq=seq, ts=ts, data=json.loads(text))
                )
                if limit is not None and len(found) >= limit:
                    return found
        return found

    async def _read_chunks(
        self,
        key: ThreadKey,
        select: Callable[[Head], list[Chunk]],
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[StreamEntry]:
        """Записи кусков заголовка; кусок убрали между чтениями (сжатие) —
        заново по свежему заголовку."""
        await self._ready()
        for _ in range(self._attempts):
            head = _live((await self._head(key))[0])
            if head is None:
                return []
            chunks = [c for c in select(head) if c.last > after_seq]
            try:
                return await self._collect(key, chunks, after_seq, limit)
            except _ChunkLost:
                continue
        raise ThreadConflict("поток треда переписывали быстрее чтения")

    async def read_stream(
        self,
        key: ThreadKey,
        stream: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[StreamEntry]:
        check_stream_name(stream)
        if limit == 0:
            return []
        return await self._read_chunks(
            key, lambda head: head.streams.get(stream, []), after_seq, limit
        )

    async def read_archive(
        self, key: ThreadKey, archive_id: str
    ) -> list[StreamEntry]:
        return await self._read_chunks(
            key, lambda head: head.archive_chunks.get(archive_id, [])
        )

    async def resolve_alias(self, alias: str) -> ThreadKey | None:
        await self._ready()
        found = await self._kv.get(keys.alias_key(alias))
        if found is None:
            return None
        owner, _at = _reservation(found.data)
        return owner if await self._listed(owner, alias) else None

    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]:
        await self._ready()
        records = await _Listing(self, query).collect()
        page = records[: query.limit]
        cursor = None
        if len(records) > query.limit:
            last = page[-1]
            cursor = (
                f"{last.info.updated_at}|{last.key.system}|{last.key.thread}"
            )
        return Page[ThreadRecord](items=page, next_cursor=cursor)

    # --- запись ----------------------------------------------------------------

    async def commit(self, key: ThreadKey, change: ThreadChange) -> Receipt:
        digest = change_digest(change)

        async def step(head: Head | None) -> _Done | _Write:
            live = _live(head)
            replayed = await self._replayed(key, live, change.op_id, digest)
            if replayed is not None:
                return _Done(replayed)
            now = self._clock()
            check_writer(live.row.lease if live else None, change.lease, now)
            if change.expected_revision != live.row.revision:
                raise ThreadConflict(
                    f"ревизия {live.row.revision}, "
                    f"ожидалась {change.expected_revision}"
                )
            new, after = live.copy(), []
            archive_id = await self._apply(key, new, change, now, after)
            new.row.revision += 1
            # Не назад: часы машины могли отступить (NTP), а инкрементальный
            # опрос списка (updated_after) потерял бы запись.
            new.row.updated_at = max(now, live.row.updated_at)
            receipt = new.row.receipt(change.op_id, archive_id, lease=True)
            item = _receipt_item(change.op_id, digest, receipt, now)
            await self._settle(key, live, new, item, after)
            return _Write(new, receipt, after)

        return await self._mutate(key, step)

    async def append_side(self, key: ThreadKey, change: SideChange) -> Receipt:
        for stream, _items in change.append:
            if check_stream_name(stream) == HISTORY:
                raise ValueError("историю треда пишет только commit")
        digest = change_digest(change)

        async def step(head: Head | None) -> _Done | _Write:
            live = _live(head)
            replayed = await self._replayed(key, live, change.op_id, digest)
            if replayed is not None:
                return _Done(replayed)
            now = self._clock()
            new = live.copy() if live else Head.fresh(key, now, head)
            check_side_writer(new.row.lease, change)
            await self._reserve_aliases(key, new, change.add_aliases, now)
            apply_side(new.row, change)
            for stream, items in change.append:
                await self._append(key, new, stream, items, now)
            receipt = new.row.receipt(change.op_id)
            item = _receipt_item(change.op_id, digest, receipt, now)
            after: list[str] = []
            await self._settle(key, live, new, item, after)
            return _Write(new, receipt, after)

        return await self._mutate(key, step)

    async def claim(self, key: ThreadKey, lease: LeaseRequest) -> ThreadRecord:
        async def step(head: Head | None) -> _Write:
            now = self._clock()
            live = _live(head)
            new = live.copy() if live else Head.fresh(key, now, head)
            new.row.grant(
                granted_lease(new.row.lease, new.row.lease_epoch, lease, now)
            )
            after: list[str] = []
            await self._reindex(key, new, after)
            return _Write(new, new.row.record(), after)

        return await self._mutate(key, step)

    async def release(self, key: ThreadKey, lease: Lease) -> None:
        async def step(head: Head | None) -> _Done | _Write:
            live = _live(head)
            if live is None or not same_lease(live.row.lease, lease):
                return _Done(None)
            new = live.copy()
            new.row.drop_lease()
            return _Write(new, None)

        await self._mutate(key, step)

    async def _thread_keys(self, key: ThreadKey, head: Head) -> list[str]:
        """Ключи данных треда — убрать после надгробия."""
        found = [
            keys.chunk_key(key, c.id)
            for chunks in (
                *head.streams.values(),
                *head.archive_chunks.values(),
            )
            for c in chunks
        ]
        found += [keys.alias_key(a) for a in head.aliases]
        if head.index is not None:
            found.append(head.index)
        for prefix in (
            keys.thread_prefix(key) + "b/",
            keys.receipt_prefix(key),
        ):
            found += await _scan_all(self._kv, prefix)
        return found

    async def delete(self, key: ThreadKey, *, op_id: str) -> None:
        del op_id  # повтор delete ничего не делает: треда уже нет

        async def step(head: Head | None) -> _Done | _Write:
            live = _live(head)
            if live is None:
                return _Done(None)
            after = await self._thread_keys(key, live)
            return _Write(live.tombstone(), None, after)

        await self._mutate(key, step)

    # --- вложения и обслуживание ------------------------------------------------

    async def put_blob(
        self, key: ThreadKey, data: bytes, *, mime: str
    ) -> BlobRef:
        await self._ready()
        name = hashlib.sha256(data).hexdigest()[:16]
        name += EXTENSIONS.get(mime, ".bin")
        await self._kv.put_any(keys.blob_key(key, name), data)
        location = f"thread-file://{key.system}/{key.thread}/{name}"
        return BlobRef(key=key, name=name, mime=mime, location=location)

    async def get_blob(self, ref: BlobRef) -> bytes:
        await self._ready()
        found = await self._kv.get(keys.blob_key(ref.key, ref.name))
        if found is None:
            raise FileNotFoundError(ref.location)
        return found.data

    async def prune_receipts(self, older_than: timedelta) -> int:
        """Убрать объекты квитанций старше ``older_than``. Квитанция
        последней записи треда живёт в его заголовке до следующей записи."""
        await self._ready()
        cutoff = self._clock() - older_than
        removed = 0
        for name in await _scan_all(self._kv, keys.RECEIPTS):
            found = await self._kv.get(name)
            if found is None:
                continue
            at = datetime.fromisoformat(json.loads(found.data)["at"])
            if at < cutoff:
                await self._kv.delete(name)
                removed += 1
        return removed

    async def sweep(self, older_than: timedelta = timedelta(hours=1)) -> int:
        """Убрать сироты прерванных записей старше ``older_than``: куски,
        на которые не ссылается заголовок, резервы алиасов без владельца,
        ключи индекса без подтверждения."""
        await self._ready()
        cutoff = self._clock() - older_than
        sweeper = _Sweeper(self, cutoff)
        return (
            await sweeper.chunks()
            + await sweeper.aliases()
            + await sweeper.index()
        )


def _receipt_item(
    op_id: str, digest: str, receipt: Receipt, now: datetime
) -> dict[str, Any]:
    return {
        "op_id": op_id,
        "digest": digest,
        "receipt": receipt.model_dump_json(),
        "at": now.isoformat(),
    }


def _receipt_of(item: dict[str, Any], op_id: str, digest: str) -> Receipt:
    if item["digest"] != digest:
        raise OperationMismatch(
            f"op_id {op_id!r} уже применён с другим содержимым"
        )
    return Receipt.model_validate_json(item["receipt"])


def _reservation(data: bytes) -> tuple[ThreadKey, datetime]:
    record = json.loads(data)
    owner = ThreadKey(system=record["system"], thread=record["thread"])
    return owner, datetime.fromisoformat(record["at"])


async def _scan_all(kv: KV, prefix: str) -> list[str]:
    found: list[str] = []
    start = ""
    while True:
        batch = await kv.scan(prefix, start_after=start, limit=_SCAN_PAGE)
        found.extend(batch)
        if len(batch) < _SCAN_PAGE:
            return found
        start = batch[-1]


class _Listing:
    """Страница списка тредов по индексу: ключи упорядочены как контракт
    (новее — раньше, затем система и тред), каждый сверяется с заголовком —
    ключ без подтверждения (прерванная запись, старое время) пропускается."""

    def __init__(self, store: KvThreadStore, query: ThreadQuery) -> None:
        self._store = store
        self._query = query
        self._after = (
            instant(query.updated_after) if query.updated_after else None
        )
        self._before = (
            instant(query.updated_before) if query.updated_before else None
        )
        self._start = ""
        if query.cursor:
            at, system, thread = query.cursor.split("|", 2)
            self._start = keys.index_key(
                instant(at), ThreadKey(system=system, thread=thread)
            )

    def _wanted_by_key(self, moment: datetime, owner: ThreadKey) -> bool:
        if self._before is not None and moment >= self._before:
            return False
        system = self._query.system
        return system is None or owner.system == system

    async def _record(
        self, index: str, owner: ThreadKey
    ) -> ThreadRecord | None:
        head = _live((await self._store._head(owner))[0])
        if head is None or head.index != index:
            return None
        workspace = self._query.workspace
        if workspace is not None and head.row.info.workspace != workspace:
            return None
        return head.row.record()

    async def _take(self, index: str, found: list[ThreadRecord]) -> bool:
        """Учесть ключ индекса; ``True`` — страница набрана или дальше
        только старее ``updated_after``."""
        moment, owner = keys.index_entry(index)
        if self._after is not None and moment <= self._after:
            return True
        if self._wanted_by_key(moment, owner):
            record = await self._record(index, owner)
            if record is not None:
                found.append(record)
        return len(found) > self._query.limit

    async def collect(self) -> list[ThreadRecord]:
        found: list[ThreadRecord] = []
        while True:
            batch = await self._store._kv.scan(
                keys.INDEX, start_after=self._start, limit=_SCAN_PAGE
            )
            for index in batch:
                self._start = index
                if await self._take(index, found):
                    return found
            if len(batch) < _SCAN_PAGE:
                return found


class _Sweeper:
    """Уборка сирот прерванных записей."""

    def __init__(self, store: KvThreadStore, cutoff: datetime) -> None:
        self._store = store
        self._kv = store._kv
        self._cutoff = cutoff

    async def _old_chunk(self, name: str) -> bool:
        found = await self._kv.get(name)
        if found is None:
            return False
        at = datetime.fromisoformat(json.loads(found.data)["at"])
        return at < self._cutoff

    async def chunks(self) -> int:
        removed = 0
        referenced: dict[str, set[str]] = {}
        for name in await _scan_all(self._kv, keys.THREADS):
            prefix, sep, chunk_id = name.rpartition("/c/")
            if not sep:
                continue
            if prefix not in referenced:
                referenced[prefix] = await self._referenced(prefix)
            if chunk_id not in referenced[prefix] and await self._old_chunk(
                name
            ):
                await self._kv.delete(name)
                removed += 1
        return removed

    async def _referenced(self, prefix: str) -> set[str]:
        found = await self._kv.get(prefix + "/h")
        head = None if found is None else _live(Head.loads(found.data))
        if head is None:
            return set()
        return {
            c.id
            for chunks in (
                *head.streams.values(),
                *head.archive_chunks.values(),
            )
            for c in chunks
        }

    async def aliases(self) -> int:
        removed = 0
        for name in await _scan_all(self._kv, keys.ALIASES):
            found = await self._kv.get(name)
            if found is None:
                continue
            owner, at = _reservation(found.data)
            alias = keys.dec(name[len(keys.ALIASES) :])
            if at < self._cutoff and not await self._store._listed(
                owner, alias
            ):
                await self._kv.delete(name)
                removed += 1
        return removed

    async def index(self) -> int:
        removed = 0
        for name in await _scan_all(self._kv, keys.INDEX):
            moment, owner = keys.index_entry(name)
            if moment >= self._cutoff:
                continue
            head = _live((await self._store._head(owner))[0])
            if head is None or head.index != name:
                await self._kv.delete(name)
                removed += 1
        return removed


__all__ = ["KvThreadStore"]
