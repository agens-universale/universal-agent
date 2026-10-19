"""``SqlThreadStore`` — хранилище тредов v2 на SQL-базе (общий движок).

Несколько инстансов пишут в одну базу: каждая запись — одна транзакция, в
которой строка треда сначала блокируется (``SELECT … FOR UPDATE``; у SQLite
— ``BEGIN IMMEDIATE`` на всю базу), а уже потом идут проверки контракта
(квитанция → аренда → ревизия). Поэтому два одинаковых ``op_id``
одновременно дают одну запись и одну квитанцию, а две записи по одной
ревизии — одну запись и ``ThreadConflict``. Дедлок или конфликт
сериализации базы — транзакция повторяется целиком.

Время — часы хранилища (``Dialect.now``: часы базы после блокировки строки
или машины); ``clock`` подменяет их в проверках.

Диалект (``gc_stores.<база>.dialect``) задаёт только запросы, время и
подключение; алгоритм здесь один на все базы.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, TypeVar

from gc_core.threads.errors import OperationMismatch, ThreadConflict
from gc_core.threads.models import (
    ArchiveInfo,
    BlobRef,
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
)
from gc_core.threads.store import ThreadStore

from gc_stores import migrations as schema
from gc_stores.common import EXTENSIONS, apply_sets, apply_side
from gc_stores.rows import (
    HISTORY,
    ThreadRow,
    dumps,
    id_key,
    message_key,
    stamp,
)
from gc_stores.sql.database import Conn, Database, in_transaction
from gc_stores.sql.dialect import Dialect
from gc_stores.sql.listing import list_query
from gc_stores.sql.queries import SqlQueries

T = TypeVar("T")

#: Колонки, которые пишут append_side и claim: статус хода и продление
#: аренды не переписывают состояние сессии.
SIDE_COLUMNS = ("runtime", "frontend", "max_seq")
LEASE_COLUMNS = ("lease_owner", "lease_epoch", "lease_expires_at")
_TIME_COLUMNS = ("created_at", "updated_at", "lease_expires_at")


class SqlThreadStore(ThreadStore):
    """Треды в SQL-базе ``database`` с диалектом ``dialect``."""

    def __init__(
        self,
        database: Database,
        dialect: Dialect,
        *,
        migrations: list[schema.Migration] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = database
        self._d = dialect
        self._q = SqlQueries(dialect)
        self._clock = clock
        self._migrations = migrations or []
        self._checked = False

    # --- схема и подключение ----------------------------------------------

    async def _ready(self) -> None:
        """Перед первой операцией — сверка версии схемы с кодом."""
        if self._checked or not self._migrations:
            return
        await self.check_schema()
        self._checked = True

    async def check_schema(self) -> None:
        """Сверить версию схемы базы с кодом; расхождение —
        ``SchemaMismatch`` с тем, что сделать."""
        await schema.check(self._db, self._d, self._migrations)

    async def ensure_schema(self) -> None:
        """Применить недостающие миграции схемы; повторный вызов безвреден.
        Всё на месте — DDL не выполняется (пользователь без права CREATE
        проходит)."""
        await schema.ensure(self._db, self._d, self._migrations)
        self._checked = True

    async def schema_version(self) -> int:
        """Применённая версия схемы базы (0 — схемы нет)."""
        async with self._db.connection() as conn:
            return await schema.applied_version(conn, self._d)

    async def _write(self, work: Callable[[Conn], Awaitable[T]]) -> T:
        await self._ready()
        return await in_transaction(self._db, work)

    async def _read(self, work: Callable[[Conn], Awaitable[T]]) -> T:
        await self._ready()
        async with self._db.connection() as conn:
            return await work(conn)

    def protected_paths(self) -> list[str]:
        """Файлы базы на диске машины (у сетевых баз — нет). Объявление
        обязательно для воркера Temporal (``worker.store_paths``)."""
        return self._db.protected_paths()

    async def close(self) -> None:
        await self._db.close()

    async def __aenter__(self) -> SqlThreadStore:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _now(self, conn: Conn) -> datetime:
        if self._clock is not None:
            return self._clock()
        return await self._d.now(conn)

    def _args(self, values: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(
            self._d.time_in(v) if isinstance(v, datetime) else v
            for v in values
        )

    def _row(self, record: Mapping[str, Any]) -> ThreadRow:
        fields = dict(record)
        for name in _TIME_COLUMNS:
            if fields.get(name) is not None:
                fields[name] = self._d.time_out(fields[name])
        return ThreadRow.from_db(fields)

    def _entry(self, record: Mapping[str, Any]) -> StreamEntry:
        return StreamEntry(
            seq=record["seq"],
            ts=stamp(self._d.time_out(record["ts"])),
            data=json.loads(record["data"]),
        )

    # --- строка треда ------------------------------------------------------

    async def _lock(self, conn: Conn, key: ThreadKey) -> ThreadRow | None:
        found = await conn.fetchrow(
            self._q.lock_thread, key.system, key.thread
        )
        return None if found is None else self._row(found)

    async def _lock_or_create(self, conn: Conn, key: ThreadKey) -> ThreadRow:
        """Строка треда под блокировкой; нет — создать (с поколением и
        эпохой удалённого треда под тем же ключом).

        Вставку мог опередить параллельный вызов, а строку — успеть удалить
        третий: тогда заново, пока строка не получена."""
        while True:
            row = await self._lock(conn, key)
            if row is not None:
                return row
            tomb = await conn.fetchrow(
                self._q.find_tombstone, key.system, key.thread
            )
            now = await self._now(conn)
            fresh = ThreadRow(key=key, created_at=now, updated_at=now)
            if tomb is not None:
                fresh.generation = tomb["generation"]
                fresh.lease_epoch = tomb["lease_epoch"]
            inserted = await conn.execute(
                self._q.insert_thread, *self._args(fresh.values())
            )
            if inserted:
                return fresh

    async def _save(
        self,
        conn: Conn,
        row: ThreadRow,
        columns: tuple[str, ...] | None = None,
    ) -> None:
        values = row.values() if columns is None else row.values(columns)
        statement = (
            self._q.update_columns()
            if columns is None
            else self._q.update_columns(columns)
        )
        await conn.execute(statement, *self._args((*values[2:], *values[:2])))

    # --- квитанции и алиасы ------------------------------------------------

    async def _replayed(
        self, conn: Conn, key: ThreadKey, op_id: str, digest: str
    ) -> Receipt | None:
        found = await conn.fetchrow(
            self._q.find_receipt, key.system, key.thread, op_id
        )
        if found is None:
            return None
        if found["digest"] != digest:
            raise OperationMismatch(
                f"op_id {op_id!r} уже применён с другим содержимым"
            )
        return Receipt.model_validate_json(found["receipt"])

    async def _remember(
        self,
        conn: Conn,
        key: ThreadKey,
        digest: str,
        receipt: Receipt,
        now: datetime,
    ) -> Receipt:
        await conn.execute(
            self._q.save_receipt,
            *self._args(
                (
                    key.system,
                    key.thread,
                    receipt.op_id,
                    digest,
                    receipt.model_dump_json(),
                    now,
                )
            ),
        )
        return receipt

    async def _add_aliases(
        self, conn: Conn, key: ThreadKey, aliases: list[str]
    ) -> None:
        """Записать алиасы; чужой — ``ValueError``, и транзакция
        откатывается целиком (ничего не записано, как требует контракт)."""
        if not aliases:
            return
        names = sorted(set(aliases))
        self._d.check_ids(*names)
        # Один порядок вставки у всех: две записи с общими алиасами на разные
        # треды ждут друг друга, а не взаимно (дедлок).
        for alias in names:
            await conn.execute(
                self._q.add_alias, alias, key.system, key.thread
            )
        foreign = await conn.fetchrow(
            self._q.foreign_aliases(len(names)),
            *names,
            key.system,
            key.thread,
        )
        if foreign is not None:
            raise ValueError(
                f"алиас {foreign['alias']!r} принадлежит треду "
                f"{foreign['system']}/{foreign['thread_id']}"
            )

    # --- потоки ------------------------------------------------------------

    async def _append(
        self,
        conn: Conn,
        row: ThreadRow,
        stream: str,
        items: list[JsonObject],
        now: datetime,
    ) -> None:
        if not items:
            return
        seq = row.max_seq.get(stream, 0)
        stamp_now = self._d.time_in(now)
        records = []
        for item in items:
            seq += 1
            records.append(
                (
                    row.key.system,
                    row.key.thread,
                    stream,
                    seq,
                    stamp_now,
                    message_key(item),
                    dumps(item),
                )
            )
        await conn.executemany(self._q.append_entry, records)
        row.max_seq[stream] = seq
        if stream == HISTORY:
            row.messages_count += len(items)

    async def _renumber(
        self,
        conn: Conn,
        row: ThreadRow,
        items: list[JsonObject],
        now: datetime,
    ) -> None:
        """История заново с seq 1; читатель с курсором узнаёт о замене по
        ``history_version``."""
        await conn.execute(
            self._q.clear_stream, row.key.system, row.key.thread, HISTORY
        )
        row.max_seq[HISTORY] = 0
        row.messages_count = 0
        await self._append(conn, row, HISTORY, items, now)
        row.history_version += 1

    async def _archive(self, conn: Conn, row: ThreadRow, now: datetime) -> str:
        archive_id = f"a{len(row.archives) + 1}"
        moved = await conn.execute(
            self._q.archive_stream,
            archive_id,
            row.key.system,
            row.key.thread,
            HISTORY,
        )
        row.archives.append(
            ArchiveInfo(
                id=archive_id, created_at=stamp(now), messages_count=moved
            )
        )
        return archive_id

    async def _discard(
        self, conn: Conn, row: ThreadRow, ids: list[str]
    ) -> None:
        if not ids:
            return
        keys = sorted({id_key(i) for i in ids})
        removed = await conn.execute(
            self._q.discard(len(keys)),
            row.key.system,
            row.key.thread,
            HISTORY,
            *keys,
        )
        row.messages_count -= removed

    async def _apply(
        self,
        conn: Conn,
        row: ThreadRow,
        change: ThreadChange,
        now: datetime,
    ) -> str | None:
        """Изменения ``commit`` в порядке контракта; вернуть id архива."""
        await self._add_aliases(conn, row.key, change.add_aliases)
        if change.reset_history:
            await self._renumber(conn, row, [], now)
            row.generation += 1
            row.drop_lease()
        archive_id = None
        compaction = change.replace_history
        if compaction is not None:
            if compaction.archive:
                archive_id = await self._archive(conn, row, now)
            await self._renumber(conn, row, compaction.messages, now)
        await self._discard(conn, row, change.discard_message_ids)
        await self._append(conn, row, HISTORY, change.append_messages, now)
        apply_sets(row, change)
        return archive_id

    # --- чтение ------------------------------------------------------------

    async def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        async def work(conn: Conn) -> ThreadRecord | None:
            found = await conn.fetchrow(
                self._q.select_thread, key.system, key.thread
            )
            return None if found is None else self._row(found).record()

        return await self._read(work)

    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]:
        sql, args = list_query(self._q, query)

        async def work(conn: Conn) -> list[Mapping[str, Any]]:
            return await conn.fetch(sql, *args)

        records = [self._row(r).record() for r in await self._read(work)]
        page = records[: query.limit]
        cursor = None
        if len(records) > query.limit:
            last = page[-1]
            cursor = (
                f"{last.info.updated_at}|{last.key.system}|{last.key.thread}"
            )
        return Page[ThreadRecord](items=page, next_cursor=cursor)

    async def read_stream(
        self,
        key: ThreadKey,
        stream: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[StreamEntry]:
        check_stream_name(stream)
        args: tuple[Any, ...] = (key.system, key.thread, stream, after_seq)
        sql = self._q.read_stream
        if limit is not None:
            sql, args = self._q.read_stream_limited, (*args, limit)

        async def work(conn: Conn) -> list[Mapping[str, Any]]:
            return await conn.fetch(sql, *args)

        return [self._entry(r) for r in await self._read(work)]

    async def read_archive(
        self, key: ThreadKey, archive_id: str
    ) -> list[StreamEntry]:
        async def work(conn: Conn) -> list[Mapping[str, Any]]:
            return await conn.fetch(
                self._q.read_archive, key.system, key.thread, archive_id
            )

        return [self._entry(r) for r in await self._read(work)]

    async def resolve_alias(self, alias: str) -> ThreadKey | None:
        async def work(conn: Conn) -> Mapping[str, Any] | None:
            return await conn.fetchrow(self._q.resolve_alias, alias)

        found = await self._read(work)
        if found is None:
            return None
        return ThreadKey(system=found["system"], thread=found["thread_id"])

    # --- запись ------------------------------------------------------------

    async def commit(self, key: ThreadKey, change: ThreadChange) -> Receipt:
        self._d.check_ids(key.system, key.thread, change.op_id)
        digest = change_digest(change)

        async def work(conn: Conn) -> Receipt:
            row = await self._lock(conn, key)
            replayed = await self._replayed(conn, key, change.op_id, digest)
            if replayed is not None:
                return replayed
            now = await self._now(conn)
            check_writer(row.lease if row else None, change.lease, now)
            if change.expected_revision != row.revision:
                raise ThreadConflict(
                    f"ревизия {row.revision}, "
                    f"ожидалась {change.expected_revision}"
                )
            archive_id = await self._apply(conn, row, change, now)
            row.revision += 1
            # Не назад: часы базы могли отступить (NTP), а инкрементальный
            # опрос списка (updated_after) потерял бы запись.
            row.updated_at = max(now, row.updated_at)
            await self._save(conn, row)
            receipt = row.receipt(change.op_id, archive_id, lease=True)
            return await self._remember(conn, key, digest, receipt, now)

        return await self._write(work)

    async def append_side(self, key: ThreadKey, change: SideChange) -> Receipt:
        for stream, _items in change.append:
            if check_stream_name(stream) == HISTORY:
                raise ValueError("историю треда пишет только commit")
        self._d.check_ids(key.system, key.thread, change.op_id)
        digest = change_digest(change)

        async def work(conn: Conn) -> Receipt:
            row = await self._lock_or_create(conn, key)
            replayed = await self._replayed(conn, key, change.op_id, digest)
            if replayed is not None:
                return replayed
            check_side_writer(row.lease, change)
            now = await self._now(conn)
            await self._add_aliases(conn, key, change.add_aliases)
            apply_side(row, change)
            for stream, items in change.append:
                await self._append(conn, row, stream, items, now)
            await self._save(conn, row, SIDE_COLUMNS)
            receipt = row.receipt(change.op_id)
            return await self._remember(conn, key, digest, receipt, now)

        return await self._write(work)

    # --- аренда и жизненный цикл ------------------------------------------

    async def claim(self, key: ThreadKey, lease: LeaseRequest) -> ThreadRecord:
        self._d.check_ids(key.system, key.thread)

        async def work(conn: Conn) -> ThreadRecord:
            row = await self._lock_or_create(conn, key)
            now = await self._now(conn)
            row.grant(granted_lease(row.lease, row.lease_epoch, lease, now))
            await self._save(conn, row, LEASE_COLUMNS)
            return row.record()

        return await self._write(work)

    async def release(self, key: ThreadKey, lease: Lease) -> None:
        async def work(conn: Conn) -> None:
            await conn.execute(
                self._q.release,
                key.system,
                key.thread,
                lease.owner,
                lease.epoch,
            )

        await self._write(work)

    async def delete(self, key: ThreadKey, *, op_id: str) -> None:
        del op_id  # повтор delete ничего не делает: треда уже нет

        async def work(conn: Conn) -> None:
            row = await self._lock(conn, key)
            if row is None:
                return
            await conn.execute(
                self._q.save_tombstone,
                key.system,
                key.thread,
                row.generation + 1,
                row.lease_epoch,
            )
            for statement in self._q.delete_thread_rows:
                await conn.execute(statement, key.system, key.thread)
            await conn.execute(self._q.delete_thread, key.system, key.thread)

        await self._write(work)

    # --- вложения и обслуживание ------------------------------------------

    async def put_blob(
        self, key: ThreadKey, data: bytes, *, mime: str
    ) -> BlobRef:
        name = hashlib.sha256(data).hexdigest()[:16]
        name += EXTENSIONS.get(mime, ".bin")

        async def work(conn: Conn) -> None:
            now = await self._now(conn)
            await conn.execute(
                self._q.put_blob,
                *self._args((key.system, key.thread, name, mime, data, now)),
            )

        await self._write(work)
        location = f"thread-file://{key.system}/{key.thread}/{name}"
        return BlobRef(key=key, name=name, mime=mime, location=location)

    async def get_blob(self, ref: BlobRef) -> bytes:
        async def work(conn: Conn) -> Any:
            return await conn.fetchval(
                self._q.get_blob, ref.key.system, ref.key.thread, ref.name
            )

        data = await self._read(work)
        if data is None:
            raise FileNotFoundError(ref.location)
        return bytes(data)

    async def prune_receipts(self, older_than: timedelta) -> int:
        async def work(conn: Conn) -> int:
            cutoff = await self._now(conn) - older_than
            return await conn.execute(
                self._q.prune_receipts, self._d.time_in(cutoff)
            )

        return await self._write(work)


__all__ = ["SqlThreadStore"]
