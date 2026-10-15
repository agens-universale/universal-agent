"""``PostgresThreadStore`` — хранилище тредов v2 на Postgres (asyncpg).

Несколько инстансов пишут в одну базу: каждая запись — одна транзакция READ
COMMITTED, в которой строка треда сначала блокируется (``SELECT … FOR
UPDATE``), а уже потом идут проверки контракта (квитанция → аренда →
ревизия). Поэтому два одинаковых ``op_id`` одновременно дают одну запись и
одну квитанцию, а две записи по одной ревизии — одну запись и
``ThreadConflict``.

Время — часы базы (``clock_timestamp()`` после блокировки строки): истечение
аренды не зависит от часов инстансов, а запись, ждавшая блокировку, не
получает время раньше той, что её держала. ``clock`` подменяет часы в
проверках.

Подключение::

    store = PostgresThreadStore("postgresql://user@host/db")
    await store.ensure_schema()
    agent = await AsyncAgent.create(settings, thread_store=store)
    ...
    await store.close()

Схема (таблицы) создаётся только ``ensure_schema()``; сама запись таблицы не
создаёт.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from importlib import resources
from typing import Any

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
    check_writer,
    granted_lease,
    instant,
)
from gc_core.threads.store import ThreadStore

from gc_stores.common import EXTENSIONS, apply_sets, apply_side
from gc_stores.postgres.queries import Queries
from gc_stores.rows import (
    HISTORY,
    TABLES,
    ThreadRow,
    dumps,
    entry_of,
    id_key,
    message_key,
    stamp,
)

_POOL_DEFAULTS = {"min_size": 1, "max_size": 10}
#: Сессия, замёрзшая посреди транзакции (инстанс остановлен), держала бы
#: блокировку строки треда бессрочно — и чужой claim не перехватил бы тред
#: даже после истечения аренды. Транзакции хранилища — миллисекунды.
_SERVER_DEFAULTS = {"idle_in_transaction_session_timeout": "60s"}
#: Колонки, которые пишут append_side и claim: статус хода и продление
#: аренды не переписывают состояние сессии (TOAST, WAL).
_SIDE_COLUMNS = ("runtime", "frontend", "max_seq")
_LEASE_COLUMNS = ("lease_owner", "lease_epoch", "lease_expires_at")


def _driver() -> Any:
    try:
        import asyncpg
    except ImportError as exc:
        raise ImportError(
            "PostgresThreadStore нужен драйвер asyncpg: установите "
            "gc-stores с extra — gc-stores[postgres] "
            "(docs/sdk/thread-store.md, раздел Postgres)"
        ) from exc
    return asyncpg


def _affected(status: str) -> int:
    """Число строк из статуса команды драйвера (``DELETE 3``)."""
    return int(status.rsplit(" ", 1)[-1])


def _schema_lock_id(schema: str) -> int:
    """Номер advisory-блокировки создания схемы: два инстанса, стартующие
    одновременно, не создают таблицы наперегонки."""
    digest = hashlib.sha256(f"gc-stores:{schema}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class PostgresThreadStore(ThreadStore):
    """Хранилище тредов в Postgres.

    ``dsn`` — строка подключения: пул создаёт и закрывает само хранилище
    (лениво, в loop первого вызова; ``pool_options`` — в
    ``asyncpg.create_pool``). ``pool`` — готовый пул встраивающего;
    закрывает его владелец. ``schema`` — схема базы, где лежат таблицы.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: Any = None,
        schema: str = "public",
        clock: Callable[[], datetime] | None = None,
        pool_options: dict[str, Any] | None = None,
    ) -> None:
        if (dsn is None) == (pool is None):
            raise ValueError("нужен ровно один из dsn и pool")
        self._sql = Queries(schema)
        self._schema = schema
        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None
        options = {**_POOL_DEFAULTS, **(pool_options or {})}
        options["server_settings"] = {
            **_SERVER_DEFAULTS,
            **options.get("server_settings", {}),
        }
        self._pool_options = options
        self._pool_lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clock = clock

    # --- подключение -----------------------------------------------------------

    async def _connected(self) -> Any:
        if not self._owns_pool:
            return self._pool
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._rebind(loop)
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:
                    self._pool = await _driver().create_pool(
                        self._dsn, **self._pool_options
                    )
        return self._pool

    def _rebind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Пул asyncpg привязан к loop. Прежний loop закрыт (частое:
        ``asyncio.run(store.ensure_schema())`` на старте) — его пул брошен,
        новый откроется здесь; живой чужой loop — понятный отказ."""
        if self._loop is not None and not self._loop.is_closed():
            raise RuntimeError(
                "PostgresThreadStore: пул открыт в другом работающем event "
                "loop; хранилище со своим пулом живёт в одном loop"
            )
        self._pool = None
        self._pool_lock = asyncio.Lock()
        self._loop = loop

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[Any]:
        pool = await self._connected()
        # Явно: контракт держится на «FOR UPDATE → свежий снимок» READ
        # COMMITTED; с default_transaction_isolation = repeatable read на
        # сервере обычные гонки давали бы SerializationError вместо
        # ThreadConflict или той же квитанции.
        async with (
            pool.acquire() as conn,
            conn.transaction(isolation="read_committed"),
        ):
            yield conn

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        pool = await self._connected()
        async with pool.acquire() as conn:
            yield conn

    async def close(self) -> None:
        """Закрыть свой пул (следующий вызов откроет новый); чужой
        (``pool=``) не трогается."""
        if not self._owns_pool or self._pool is None:
            return
        if self._loop is not asyncio.get_running_loop():
            self._rebind(asyncio.get_running_loop())
            return
        pool, self._pool = self._pool, None
        await pool.close()

    async def __aenter__(self) -> PostgresThreadStore:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def ensure_schema(self) -> None:
        """Создать схему и таблицы, если их нет; повторный вызов безвреден.

        Всё на месте — DDL не выполняется: Postgres проверяет право
        ``CREATE`` раньше, чем ``IF NOT EXISTS``, и пользователь приложения
        без этого права (таблицы создал администратор) иначе упал бы.
        """
        ddl = (
            resources.files("gc_stores.postgres")
            .joinpath("schema.sql")
            .read_text(encoding="utf-8")
        )
        async with self._transaction() as conn:
            present = await conn.fetchval(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = $1 AND tablename = ANY($2::text[])",
                self._schema,
                list(TABLES),
            )
            if present == len(TABLES):
                return
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)",
                _schema_lock_id(self._schema),
            )
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_namespace WHERE nspname = $1", self._schema
            )
            if not exists:
                await conn.execute(f"CREATE SCHEMA {self._sql.schema_ident}")
            await conn.execute(
                f"SET LOCAL search_path TO {self._sql.schema_ident}"
            )
            await conn.execute(ddl)

    async def _now(self, conn: Any) -> datetime:
        """Часы базы в момент вызова (не начала транзакции): запись, ждавшая
        блокировку, иначе получила бы время раньше той, что её держала."""
        if self._clock is not None:
            return self._clock()
        return await conn.fetchval("SELECT clock_timestamp()")

    # --- строка треда ------------------------------------------------------------

    async def _lock(self, conn: Any, key: ThreadKey) -> ThreadRow | None:
        row = await conn.fetchrow(
            self._sql.lock_thread, key.system, key.thread
        )
        return None if row is None else ThreadRow.from_db(row)

    async def _lock_or_create(self, conn: Any, key: ThreadKey) -> ThreadRow:
        """Строка треда под блокировкой; нет — создать (с поколением и
        эпохой удалённого треда под тем же ключом).

        Вставку мог опередить параллельный вызов, а строку — успеть удалить
        третий: тогда заново, пока строка не получена (каждый запрос READ
        COMMITTED видит свежий снимок)."""
        while True:
            row = await self._lock(conn, key)
            if row is not None:
                return row
            tomb = await conn.fetchrow(
                self._sql.find_tombstone, key.system, key.thread
            )
            now = await self._now(conn)
            fresh = ThreadRow(key=key, created_at=now, updated_at=now)
            if tomb is not None:
                fresh.generation = tomb["generation"]
                fresh.lease_epoch = tomb["lease_epoch"]
            if await conn.fetchval(self._sql.insert_thread, *fresh.values()):
                return fresh

    async def _save(
        self, conn: Any, row: ThreadRow, columns: tuple[str, ...] | None = None
    ) -> None:
        if columns is None:
            await conn.execute(self._sql.update_thread, *row.values())
            return
        await conn.execute(
            self._sql.update_columns(columns), *row.values(columns)
        )

    # --- квитанции и алиасы ---------------------------------------------------------

    async def _replayed(
        self, conn: Any, key: ThreadKey, op_id: str, digest: str
    ) -> Receipt | None:
        found = await conn.fetchrow(
            self._sql.find_receipt, key.system, key.thread, op_id
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
        conn: Any,
        key: ThreadKey,
        digest: str,
        receipt: Receipt,
        now: datetime,
    ) -> Receipt:
        await conn.execute(
            self._sql.save_receipt,
            key.system,
            key.thread,
            receipt.op_id,
            digest,
            receipt.model_dump_json(),
            now,
        )
        return receipt

    async def _add_aliases(
        self, conn: Any, key: ThreadKey, aliases: list[str]
    ) -> None:
        """Записать алиасы; чужой — ``ValueError``, и транзакция
        откатывается целиком (ничего не записано, как требует контракт)."""
        if not aliases:
            return
        # Один порядок вставки у всех: две записи с общими алиасами на разные
        # треды ждут друг друга, а не взаимно (дедлок).
        args = (key.system, key.thread, sorted(set(aliases)))
        await conn.execute(self._sql.add_aliases, *args)
        foreign = await conn.fetchrow(self._sql.foreign_alias, *args)
        if foreign is not None:
            raise ValueError(
                f"алиас {foreign['alias']!r} принадлежит треду "
                f"{foreign['system']}/{foreign['thread_id']}"
            )

    # --- потоки -------------------------------------------------------------------

    async def _append(
        self,
        conn: Any,
        row: ThreadRow,
        stream: str,
        items: list[JsonObject],
        now: datetime,
    ) -> None:
        if not items:
            return
        seq = row.max_seq.get(stream, 0)
        records = []
        for item in items:
            seq += 1
            records.append(
                (
                    row.key.system,
                    row.key.thread,
                    stream,
                    seq,
                    now,
                    message_key(item),
                    dumps(item),
                )
            )
        await conn.executemany(self._sql.append_entry, records)
        row.max_seq[stream] = seq
        if stream == HISTORY:
            row.messages_count += len(items)

    async def _renumber(
        self,
        conn: Any,
        row: ThreadRow,
        items: list[JsonObject],
        now: datetime,
    ) -> None:
        """История заново с seq 1; читатель с курсором узнаёт о замене по
        ``history_version``."""
        await conn.execute(
            self._sql.clear_stream, row.key.system, row.key.thread, HISTORY
        )
        row.max_seq[HISTORY] = 0
        row.messages_count = 0
        await self._append(conn, row, HISTORY, items, now)
        row.history_version += 1

    async def _archive(self, conn: Any, row: ThreadRow, now: datetime) -> str:
        archive_id = f"a{len(row.archives) + 1}"
        status = await conn.execute(
            self._sql.archive_stream,
            row.key.system,
            row.key.thread,
            HISTORY,
            archive_id,
        )
        row.archives.append(
            ArchiveInfo(
                id=archive_id,
                created_at=stamp(now),
                messages_count=_affected(status),
            )
        )
        return archive_id

    async def _discard(
        self, conn: Any, row: ThreadRow, ids: list[str]
    ) -> None:
        if not ids:
            return
        status = await conn.execute(
            self._sql.discard_entries,
            row.key.system,
            row.key.thread,
            HISTORY,
            [id_key(i) for i in ids],
        )
        row.messages_count -= _affected(status)

    # --- применение commit ------------------------------------------------------

    async def _apply(
        self,
        conn: Any,
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

    # --- чтение ---------------------------------------------------------------------

    async def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        async with self._connection() as conn:
            row = await conn.fetchrow(
                self._sql.select_thread, key.system, key.thread
            )
        return None if row is None else ThreadRow.from_db(row).record()

    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]:
        after = before = cursor_at = None
        cursor_system = cursor_thread = None
        if query.updated_after:
            after = instant(query.updated_after)
        if query.updated_before:
            before = instant(query.updated_before)
        if query.cursor:
            at, cursor_system, cursor_thread = query.cursor.split("|", 2)
            cursor_at = instant(at)
        async with self._connection() as conn:
            rows = await conn.fetch(
                self._sql.list_threads,
                query.system,
                query.workspace,
                after,
                before,
                cursor_at,
                cursor_system,
                cursor_thread,
                query.limit + 1,
            )
        records = [ThreadRow.from_db(row).record() for row in rows]
        page = records[: query.limit]
        next_cursor = None
        if len(records) > query.limit:
            last = page[-1]
            next_cursor = (
                f"{last.info.updated_at}|{last.key.system}|{last.key.thread}"
            )
        return Page[ThreadRecord](items=page, next_cursor=next_cursor)

    async def read_stream(
        self,
        key: ThreadKey,
        stream: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[StreamEntry]:
        check_stream_name(stream)
        async with self._connection() as conn:
            rows = await conn.fetch(
                self._sql.read_stream,
                key.system,
                key.thread,
                stream,
                after_seq,
                limit,
            )
        return [entry_of(row) for row in rows]

    async def read_archive(
        self, key: ThreadKey, archive_id: str
    ) -> list[StreamEntry]:
        async with self._connection() as conn:
            rows = await conn.fetch(
                self._sql.read_archive, key.system, key.thread, archive_id
            )
        return [entry_of(row) for row in rows]

    async def resolve_alias(self, alias: str) -> ThreadKey | None:
        async with self._connection() as conn:
            row = await conn.fetchrow(self._sql.resolve_alias, alias)
        if row is None:
            return None
        return ThreadKey(system=row["system"], thread=row["thread_id"])

    # --- запись ---------------------------------------------------------------------

    async def commit(self, key: ThreadKey, change: ThreadChange) -> Receipt:
        digest = change_digest(change)
        async with self._transaction() as conn:
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

    async def append_side(self, key: ThreadKey, change: SideChange) -> Receipt:
        for stream, _items in change.append:
            if check_stream_name(stream) == HISTORY:
                raise ValueError("историю треда пишет только commit")
        digest = change_digest(change)
        async with self._transaction() as conn:
            row = await self._lock_or_create(conn, key)
            replayed = await self._replayed(conn, key, change.op_id, digest)
            if replayed is not None:
                return replayed
            now = await self._now(conn)
            await self._add_aliases(conn, key, change.add_aliases)
            apply_side(row, change)
            for stream, items in change.append:
                await self._append(conn, row, stream, items, now)
            await self._save(conn, row, _SIDE_COLUMNS)
            receipt = row.receipt(change.op_id)
            return await self._remember(conn, key, digest, receipt, now)

    # --- аренда и жизненный цикл ---------------------------------------------------

    async def claim(self, key: ThreadKey, lease: LeaseRequest) -> ThreadRecord:
        async with self._transaction() as conn:
            row = await self._lock_or_create(conn, key)
            now = await self._now(conn)
            row.grant(granted_lease(row.lease, row.lease_epoch, lease, now))
            await self._save(conn, row, _LEASE_COLUMNS)
            return row.record()

    async def release(self, key: ThreadKey, lease: Lease) -> None:
        async with self._connection() as conn:
            await conn.execute(
                self._sql.release,
                key.system,
                key.thread,
                lease.owner,
                lease.epoch,
            )

    async def delete(self, key: ThreadKey, *, op_id: str) -> None:
        del op_id  # повтор delete ничего не делает: треда уже нет
        async with self._transaction() as conn:
            row = await self._lock(conn, key)
            if row is None:
                return
            await conn.execute(
                self._sql.save_tombstone,
                key.system,
                key.thread,
                row.generation + 1,
                row.lease_epoch,
            )
            for statement in self._sql.delete_thread_rows:
                await conn.execute(statement, key.system, key.thread)
            await conn.execute(self._sql.delete_thread, key.system, key.thread)

    # --- вложения и обслуживание ---------------------------------------------------

    async def put_blob(
        self, key: ThreadKey, data: bytes, *, mime: str
    ) -> BlobRef:
        name = hashlib.sha256(data).hexdigest()[:16]
        name += EXTENSIONS.get(mime, ".bin")
        async with self._connection() as conn:
            now = await self._now(conn)
            await conn.execute(
                self._sql.put_blob,
                key.system,
                key.thread,
                name,
                mime,
                data,
                now,
            )
        location = f"thread-file://{key.system}/{key.thread}/{name}"
        return BlobRef(key=key, name=name, mime=mime, location=location)

    async def get_blob(self, ref: BlobRef) -> bytes:
        async with self._connection() as conn:
            data = await conn.fetchval(
                self._sql.get_blob, ref.key.system, ref.key.thread, ref.name
            )
        if data is None:
            raise FileNotFoundError(ref.location)
        return bytes(data)

    async def prune_receipts(self, older_than: timedelta) -> int:
        async with self._connection() as conn:
            cutoff = await self._now(conn) - older_than
            status = await conn.execute(self._sql.prune_receipts, cutoff)
        return _affected(status)
