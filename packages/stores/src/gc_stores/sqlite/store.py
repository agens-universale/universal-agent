"""``SQLiteThreadStore`` — хранилище тредов v2 в одном файле SQLite.

Для одного воркера (или нескольких процессов на одной машине): запись —
транзакция ``BEGIN IMMEDIATE`` (база заперта на запись и между
процессами), внутри неё проверки контракта (квитанция → аренда → ревизия) и
применение. Драйвер — ``sqlite3`` из стандартной библиотеки; синхронные
вызовы идут в потоке, event loop не ждёт диск.

Время — часы машины (``clock``), формат ISO-8601 UTC фиксированной ширины.
Файлы треда (``write_thread_file``/``read_thread_file``) — в той же базе:
картинки и медиа ядра под Temporal живут с тредом, а не на диске воркера.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from gc_core.thread_files import check_name
from gc_core.threads.errors import OperationMismatch, ThreadConflict
from gc_core.threads.models import (
    ArchiveInfo,
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
    change_digest,
    check_stream_name,
)
from gc_core.threads.rules import (
    check_writer,
    granted_lease,
    instant,
    same_lease,
)
from gc_core.threads.store import ThreadStore

from gc_stores.common import EXTENSIONS, apply_sets, apply_side
from gc_stores.rows import (
    HISTORY,
    THREAD_COLUMNS,
    ThreadRow,
    dumps,
    id_key,
    message_key,
)

_TIME_COLUMNS = frozenset({"created_at", "updated_at", "lease_expires_at"})
_BY_THREAD = "system = ? AND thread_id = ?"
_SIDE_COLUMNS = ("runtime", "frontend", "max_seq")
_LEASE_COLUMNS = ("lease_owner", "lease_epoch", "lease_expires_at")
_SELECT_THREAD = (
    f"SELECT system, thread_id, {', '.join(THREAD_COLUMNS)} FROM gc_threads"
)
_FILE_MIME = "application/octet-stream"


def _stamp(moment: datetime) -> str:
    """Время в базе: UTC, микросекунды всегда — строки сравниваются как
    моменты."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _sql(value: Any) -> Any:
    return _stamp(value) if isinstance(value, datetime) else value


def _row(record: sqlite3.Row) -> ThreadRow:
    fields = {
        name: (
            datetime.fromisoformat(record[name])
            if name in _TIME_COLUMNS and record[name] is not None
            else record[name]
        )
        for name in record.keys()  # noqa: SIM118  # sqlite3.Row: обход — по значениям
    }
    return ThreadRow.from_db(fields)


def _entry(record: sqlite3.Row) -> StreamEntry:
    return StreamEntry(
        seq=record["seq"],
        ts=datetime.fromisoformat(record["ts"]).isoformat(),
        data=json.loads(record["data"]),
    )


class SQLiteThreadStore(ThreadStore):
    """Треды в файле ``path``; ``clock`` — часы хранилища (аренда,
    квитанции, штампы)."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = str(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        ddl = (
            resources.files("gc_stores.sqlite")
            .joinpath("schema.sql")
            .read_text(encoding="utf-8")
        )
        with closing(self._connect()) as conn:
            conn.executescript(ddl)

    def protected_paths(self) -> list[str]:
        """Файл базы и её журналы: агенту к ним нельзя прикасаться."""
        return [
            self.path,
            *(
                f"{self.path}{suffix}"
                for suffix in ("-wal", "-shm", "-journal")
            ),
        ]

    # --- подключение ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # FULL: запись треда переживает падение машины, а не только процесса.
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _in_transaction(self, work, *args):
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = work(conn, *args)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

    def _reading(self, work, *args):
        with closing(self._connect()) as conn:
            return work(conn, *args)

    async def _write(self, work, *args):
        return await asyncio.to_thread(self._in_transaction, work, *args)

    async def _read(self, work, *args):
        return await asyncio.to_thread(self._reading, work, *args)

    # --- строка треда -----------------------------------------------------

    @staticmethod
    def _load(conn, key: ThreadKey) -> ThreadRow | None:
        found = conn.execute(
            f"{_SELECT_THREAD} WHERE {_BY_THREAD}", (key.system, key.thread)
        ).fetchone()
        return None if found is None else _row(found)

    def _load_or_create(self, conn, key: ThreadKey) -> ThreadRow:
        row = self._load(conn, key)
        if row is not None:
            return row
        now = self._clock()
        row = ThreadRow(key=key, created_at=now, updated_at=now)
        tomb = conn.execute(
            f"SELECT generation, lease_epoch FROM gc_tombstones "
            f"WHERE {_BY_THREAD}",
            (key.system, key.thread),
        ).fetchone()
        if tomb is not None:
            row.generation, row.lease_epoch = tomb[0], tomb[1]
        names = ("system", "thread_id", *THREAD_COLUMNS)
        conn.execute(
            f"INSERT INTO gc_threads ({', '.join(names)}) "
            f"VALUES ({', '.join('?' * len(names))})",
            tuple(_sql(v) for v in row.values()),
        )
        return row

    @staticmethod
    def _save(conn, row: ThreadRow, columns=THREAD_COLUMNS) -> None:
        values = row.values(columns)
        sets = ", ".join(f"{name} = ?" for name in columns)
        conn.execute(
            f"UPDATE gc_threads SET {sets} WHERE {_BY_THREAD}",
            (*(_sql(v) for v in values[2:]), *values[:2]),
        )

    # --- квитанции и алиасы ----------------------------------------------

    @staticmethod
    def _replayed(conn, key, op_id: str, digest: str) -> Receipt | None:
        found = conn.execute(
            f"SELECT digest, receipt FROM gc_receipts "
            f"WHERE {_BY_THREAD} AND op_id = ?",
            (key.system, key.thread, op_id),
        ).fetchone()
        if found is None:
            return None
        if found["digest"] != digest:
            raise OperationMismatch(
                f"op_id {op_id!r} уже применён с другим содержимым"
            )
        return Receipt.model_validate_json(found["receipt"])

    def _remember(self, conn, key, digest: str, receipt: Receipt) -> Receipt:
        conn.execute(
            "INSERT INTO gc_receipts "
            "(system, thread_id, op_id, digest, receipt, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                key.system,
                key.thread,
                receipt.op_id,
                digest,
                receipt.model_dump_json(),
                _stamp(self._clock()),
            ),
        )
        return receipt

    @staticmethod
    def _add_aliases(conn, key: ThreadKey, aliases: list[str]) -> None:
        """Алиасы треда; чужой — ``ValueError`` (транзакция откатится)."""
        for alias in sorted(set(aliases)):
            owner = conn.execute(
                "SELECT system, thread_id FROM gc_aliases WHERE alias = ?",
                (alias,),
            ).fetchone()
            if owner is None:
                conn.execute(
                    "INSERT INTO gc_aliases (alias, system, thread_id) "
                    "VALUES (?, ?, ?)",
                    (alias, key.system, key.thread),
                )
            elif (owner[0], owner[1]) != (key.system, key.thread):
                raise ValueError(
                    f"алиас {alias!r} принадлежит треду {owner[0]}/{owner[1]}"
                )

    # --- потоки ---------------------------------------------------------

    @staticmethod
    def _append(conn, row: ThreadRow, stream: str, items, now) -> None:
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
                    _stamp(now),
                    message_key(item),
                    dumps(item),
                )
            )
        conn.executemany(
            "INSERT INTO gc_stream_entries "
            "(system, thread_id, stream, seq, ts, msg_key, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            records,
        )
        if records:
            row.max_seq[stream] = seq
        if stream == HISTORY:
            row.messages_count += len(records)

    def _renumber(self, conn, row: ThreadRow, items, now) -> None:
        conn.execute(
            f"DELETE FROM gc_stream_entries WHERE {_BY_THREAD} AND stream = ?",
            (row.key.system, row.key.thread, HISTORY),
        )
        row.max_seq[HISTORY] = 0
        row.messages_count = 0
        self._append(conn, row, HISTORY, items, now)
        row.history_version += 1

    @staticmethod
    def _archive(conn, row: ThreadRow, now) -> str:
        archive_id = f"a{len(row.archives) + 1}"
        moved = conn.execute(
            "INSERT INTO gc_archives "
            "(system, thread_id, archive_id, seq, ts, data) "
            "SELECT system, thread_id, ?, seq, ts, data FROM gc_stream_entries "
            f"WHERE {_BY_THREAD} AND stream = ?",
            (archive_id, row.key.system, row.key.thread, HISTORY),
        ).rowcount
        row.archives.append(
            ArchiveInfo(
                id=archive_id, created_at=now.isoformat(), messages_count=moved
            )
        )
        return archive_id

    @staticmethod
    def _discard(conn, row: ThreadRow, ids: list[str]) -> None:
        for message_id in ids:
            row.messages_count -= conn.execute(
                "DELETE FROM gc_stream_entries "
                f"WHERE {_BY_THREAD} AND stream = ? AND msg_key = ?",
                (row.key.system, row.key.thread, HISTORY, id_key(message_id)),
            ).rowcount

    def _apply(self, conn, row: ThreadRow, change: ThreadChange, now):
        self._add_aliases(conn, row.key, change.add_aliases)
        if change.reset_history:
            self._renumber(conn, row, [], now)
            row.generation += 1
            row.drop_lease()
        archive_id = None
        compaction = change.replace_history
        if compaction is not None:
            if compaction.archive:
                archive_id = self._archive(conn, row, now)
            self._renumber(conn, row, compaction.messages, now)
        self._discard(conn, row, change.discard_message_ids)
        self._append(conn, row, HISTORY, change.append_messages, now)
        apply_sets(row, change)
        return archive_id

    # --- чтение --------------------------------------------------------------

    async def get_thread(self, key: ThreadKey) -> ThreadRecord | None:
        row = await self._read(self._load, key)
        return None if row is None else row.record()

    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]:
        rows = await self._read(_listed, query)
        records = [row.record() for row in rows]
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

        def work(conn):
            return conn.execute(
                "SELECT seq, ts, data FROM gc_stream_entries "
                f"WHERE {_BY_THREAD} AND stream = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (key.system, key.thread, stream, after_seq, limit or -1),
            ).fetchall()

        return [_entry(r) for r in await self._read(work)]

    async def read_archive(
        self, key: ThreadKey, archive_id: str
    ) -> list[StreamEntry]:
        def work(conn):
            return conn.execute(
                "SELECT seq, ts, data FROM gc_archives "
                f"WHERE {_BY_THREAD} AND archive_id = ? ORDER BY seq",
                (key.system, key.thread, archive_id),
            ).fetchall()

        return [_entry(r) for r in await self._read(work)]

    async def resolve_alias(self, alias: str) -> ThreadKey | None:
        def work(conn):
            return conn.execute(
                "SELECT system, thread_id FROM gc_aliases WHERE alias = ?",
                (alias,),
            ).fetchone()

        found = await self._read(work)
        return (
            None
            if found is None
            else ThreadKey(system=found[0], thread=found[1])
        )

    # --- запись ------------------------------------------------------------

    async def commit(self, key: ThreadKey, change: ThreadChange) -> Receipt:
        return await self._write(
            self._commit, key, change, change_digest(change)
        )

    def _commit(self, conn, key, change: ThreadChange, digest: str) -> Receipt:
        row = self._load(conn, key)
        replayed = self._replayed(conn, key, change.op_id, digest)
        if replayed is not None:
            return replayed
        now = self._clock()
        check_writer(row.lease if row else None, change.lease, now)
        if change.expected_revision != row.revision:
            raise ThreadConflict(
                f"ревизия {row.revision}, ожидалась {change.expected_revision}"
            )
        archive_id = self._apply(conn, row, change, now)
        row.revision += 1
        row.updated_at = max(now, row.updated_at)
        self._save(conn, row)
        return self._remember(
            conn,
            key,
            digest,
            row.receipt(change.op_id, archive_id, lease=True),
        )

    async def append_side(self, key: ThreadKey, change: SideChange) -> Receipt:
        for stream, _items in change.append:
            if check_stream_name(stream) == HISTORY:
                raise ValueError("историю треда пишет только commit")
        return await self._write(
            self._append_side, key, change, change_digest(change)
        )

    def _append_side(self, conn, key, change: SideChange, digest: str):
        row = self._load_or_create(conn, key)
        replayed = self._replayed(conn, key, change.op_id, digest)
        if replayed is not None:
            return replayed
        now = self._clock()
        self._add_aliases(conn, key, change.add_aliases)
        apply_side(row, change)
        for stream, items in change.append:
            self._append(conn, row, stream, items, now)
        self._save(conn, row, _SIDE_COLUMNS)
        return self._remember(conn, key, digest, row.receipt(change.op_id))

    # --- аренда и жизненный цикл ----------------------------------------------

    async def claim(self, key: ThreadKey, lease: LeaseRequest) -> ThreadRecord:
        def work(conn):
            row = self._load_or_create(conn, key)
            now = self._clock()
            row.grant(granted_lease(row.lease, row.lease_epoch, lease, now))
            self._save(conn, row, _LEASE_COLUMNS)
            return row.record()

        return await self._write(work)

    async def release(self, key: ThreadKey, lease: Lease) -> None:
        def work(conn):
            row = self._load(conn, key)
            if row is not None and same_lease(row.lease, lease):
                row.drop_lease()
                self._save(conn, row, _LEASE_COLUMNS)

        await self._write(work)

    async def delete(self, key: ThreadKey, *, op_id: str) -> None:
        del op_id  # повтор delete ничего не делает: треда уже нет
        await self._write(_delete, key)

    # --- вложения и обслуживание ----------------------------------------------

    async def put_blob(
        self, key: ThreadKey, data: bytes, *, mime: str
    ) -> BlobRef:
        name = hashlib.sha256(data).hexdigest()[:16]
        name += EXTENSIONS.get(mime, ".bin")
        await self._write(self._store_file, key, name, mime, data, False)
        location = f"thread-file://{key.system}/{key.thread}/{name}"
        return BlobRef(key=key, name=name, mime=mime, location=location)

    def _store_file(self, conn, key, name, mime, data, replace: bool) -> None:
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        conn.execute(
            f"{verb} INTO gc_blobs "
            "(system, thread_id, name, mime, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key.system, key.thread, name, mime, data, _stamp(self._clock())),
        )

    async def get_blob(self, ref: BlobRef) -> bytes:
        data = await self._read(_blob, ref.key, ref.name)
        if data is None:
            raise FileNotFoundError(ref.location)
        return data

    async def prune_receipts(self, older_than: timedelta) -> int:
        cutoff = _stamp(self._clock() - older_than)

        def work(conn):
            return conn.execute(
                "DELETE FROM gc_receipts WHERE created_at < ?", (cutoff,)
            ).rowcount

        return await self._write(work)

    # --- файлы треда (картинки и медиа ядра) --------------------------------

    def write_thread_file(
        self, system_id: str, thread_id: str, name: str, data: bytes
    ) -> None:
        key = ThreadKey(system=system_id, thread=thread_id)
        self._in_transaction(
            self._store_file, key, check_name(name), _FILE_MIME, data, True
        )

    def read_thread_file(
        self, system_id: str, thread_id: str, name: str
    ) -> bytes | None:
        key = ThreadKey(system=system_id, thread=thread_id)
        return self._reading(_blob, key, check_name(name))


def _blob(conn, key: ThreadKey, name: str) -> bytes | None:
    found = conn.execute(
        f"SELECT data FROM gc_blobs WHERE {_BY_THREAD} AND name = ?",
        (key.system, key.thread, name),
    ).fetchone()
    return None if found is None else bytes(found[0])


def _delete(conn, key: ThreadKey) -> None:
    row = SQLiteThreadStore._load(conn, key)
    if row is None:
        return
    conn.execute(
        "INSERT INTO gc_tombstones (system, thread_id, generation, lease_epoch) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (system, thread_id) DO UPDATE SET "
        "generation = excluded.generation, lease_epoch = excluded.lease_epoch",
        (key.system, key.thread, row.generation + 1, row.lease_epoch),
    )
    for table in (
        "gc_stream_entries",
        "gc_archives",
        "gc_blobs",
        "gc_aliases",
        "gc_receipts",
        "gc_threads",
    ):
        conn.execute(
            f"DELETE FROM {table} WHERE {_BY_THREAD}", (key.system, key.thread)
        )


def _listed(conn, query: ThreadQuery) -> list[ThreadRow]:
    """Строки списка: фильтры, порядок (updated_at ↓, system, thread_id —
    как ``rules.page_of``), курсор, на одну больше страницы."""
    where, params = ["1 = 1"], []
    conditions = (
        (query.system, "system = ?"),
        (query.workspace, "workspace = ?"),
    )
    for value, clause in conditions:
        if value is not None:
            where.append(clause)
            params.append(value)
    if query.updated_after:
        where.append("updated_at > ?")
        params.append(_stamp(instant(query.updated_after)))
    if query.updated_before:
        where.append("updated_at < ?")
        params.append(_stamp(instant(query.updated_before)))
    if query.cursor:
        at, system, thread = query.cursor.split("|", 2)
        moment = _stamp(instant(at))
        where.append(
            "(updated_at < ? OR (updated_at = ? AND "
            "(system > ? OR (system = ? AND thread_id > ?))))"
        )
        params.extend([moment, moment, system, system, thread])
    found = conn.execute(
        f"{_SELECT_THREAD} WHERE {' AND '.join(where)} "
        "ORDER BY updated_at DESC, system, thread_id LIMIT ?",
        (*params, query.limit + 1),
    ).fetchall()
    return [_row(r) for r in found]


__all__ = ["SQLiteThreadStore"]
