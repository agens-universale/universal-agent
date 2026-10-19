"""Тексты запросов SQL-движка для диалекта: таблицы, колонки, плейсхолдеры.

Каждый параметр — один раз и по порядку (см. ``dialect.py``). Имена колонок
— в кавычках диалекта: у MySQL ``system`` и ``usage`` — зарезервированные
слова. Данные в текст SQL не попадают — только имена из кода.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from gc_stores.rows import THREAD_COLUMNS
from gc_stores.sql.dialect import Dialect

ENTRY_COLUMNS = (
    "system",
    "thread_id",
    "stream",
    "seq",
    "ts",
    "msg_key",
    "data",
)
BLOB_COLUMNS = ("system", "thread_id", "name", "mime", "data", "created_at")
RECEIPT_COLUMNS = (
    "system",
    "thread_id",
    "op_id",
    "digest",
    "receipt",
    "created_at",
)
TOMBSTONE_COLUMNS = ("system", "thread_id", "generation", "lease_epoch")
ALIAS_COLUMNS = ("alias", "system", "thread_id")
#: Таблицы с данными треда — удаляются вместе с ним.
THREAD_TABLES = (
    "gc_stream_entries",
    "gc_archives",
    "gc_blobs",
    "gc_aliases",
    "gc_receipts",
)


class SqlQueries:
    """Запросы движка для диалекта ``d``."""

    def __init__(self, d: Dialect) -> None:
        self.d = d
        self.c = d.ident
        self.threads = d.table("gc_threads")
        self.entries = d.table("gc_stream_entries")
        self.archives = d.table("gc_archives")
        self.blobs = d.table("gc_blobs")
        self.aliases = d.table("gc_aliases")
        self.receipts = d.table("gc_receipts")
        self.tombstones = d.table("gc_tombstones")
        self.thread_columns = self.names(
            ("system", "thread_id", *THREAD_COLUMNS)
        )
        self._updates: dict[tuple[str, ...], str] = {}
        self._threads()
        self._streams()
        self._side_tables()

    def names(self, columns: Sequence[str]) -> str:
        return ", ".join(self.c(name) for name in columns)

    def by_thread(self, p: Callable[[], str]) -> str:
        return f"{self.c('system')} = {p()} AND {self.c('thread_id')} = {p()}"

    def insert(self, table: str, columns: Sequence[str]) -> str:
        marks = self.d.params()
        values = ", ".join(marks() for _ in columns)
        return f"INSERT INTO {table} ({self.names(columns)}) VALUES ({values})"

    def _threads(self) -> None:
        d, c = self.d, self.c
        p = d.params()
        self.select_thread = (
            f"SELECT {self.thread_columns} FROM {self.threads} "
            f"WHERE {self.by_thread(p)}"
        )
        self.lock_thread = self.select_thread + d.lock_suffix
        self.insert_thread = d.insert_ignore(
            self.threads, ("system", "thread_id", *THREAD_COLUMNS)
        )
        p = d.params()
        self.release = (
            f"UPDATE {self.threads} SET {c('lease_owner')} = NULL, "
            f"{c('lease_expires_at')} = NULL WHERE {self.by_thread(p)} "
            f"AND {c('lease_owner')} = {p()} AND {c('lease_epoch')} = {p()}"
        )

    def update_columns(self, columns: tuple[str, ...] = THREAD_COLUMNS) -> str:
        """``UPDATE`` колонок строки треда; параметры — значения колонок,
        затем ``system``, ``thread_id``."""
        statement = self._updates.get(columns)
        if statement is None:
            p = self.d.params()
            sets = ", ".join(f"{self.c(name)} = {p()}" for name in columns)
            statement = (
                f"UPDATE {self.threads} SET {sets} WHERE {self.by_thread(p)}"
            )
            self._updates[columns] = statement
        return statement

    def _streams(self) -> None:
        d, c = self.d, self.c
        entry = self.names(("seq", "ts", "data"))
        p = d.params()
        self.read_stream = (
            f"SELECT {entry} FROM {self.entries} WHERE {self.by_thread(p)} "
            f"AND {c('stream')} = {p()} AND {c('seq')} > {p()} "
            f"ORDER BY {c('seq')}"
        )
        self.read_stream_limited = f"{self.read_stream} LIMIT {p()}"
        # seq выдаёт строка треда под блокировкой: дубль — ошибка движка,
        # а не тихий пропуск.
        self.append_entry = self.insert(self.entries, ENTRY_COLUMNS)
        p = d.params()
        self.clear_stream = (
            f"DELETE FROM {self.entries} "
            f"WHERE {self.by_thread(p)} AND {c('stream')} = {p()}"
        )
        p = d.params()
        archive_id = d.cast_text(p())
        moved = self.names(("system", "thread_id"))
        self.archive_stream = (
            f"INSERT INTO {self.archives} ("
            f"{self.names(('system', 'thread_id', 'archive_id', 'seq', 'ts', 'data'))}"
            f") SELECT {moved}, {archive_id}, {entry} FROM {self.entries} "
            f"WHERE {self.by_thread(p)} AND {c('stream')} = {p()}"
        )
        p = d.params()
        self.read_archive = (
            f"SELECT {entry} FROM {self.archives} WHERE {self.by_thread(p)} "
            f"AND {c('archive_id')} = {p()} ORDER BY {c('seq')}"
        )

    def discard(self, count: int) -> str:
        """Удалить записи потока по ключам id (``msg_key``)."""
        c, p = self.c, self.d.params()
        by_thread = self.by_thread(p)
        stream = p()
        keys = ", ".join(p() for _ in range(count))
        return (
            f"DELETE FROM {self.entries} WHERE {by_thread} "
            f"AND {c('stream')} = {stream} AND {c('msg_key')} IN ({keys})"
        )

    def foreign_aliases(self, count: int) -> str:
        """Первый по имени из алиасов, принадлежащий другому треду;
        параметры — алиасы, затем ``system``, ``thread_id``."""
        c, p = self.c, self.d.params()
        names = ", ".join(p() for _ in range(count))
        return (
            f"SELECT {self.names(ALIAS_COLUMNS)} FROM {self.aliases} "
            f"WHERE {c('alias')} IN ({names}) "
            f"AND ({c('system')} <> {p()} OR {c('thread_id')} <> {p()}) "
            f"ORDER BY {c('alias')} LIMIT 1"
        )

    def _side_tables(self) -> None:
        d, c = self.d, self.c
        self.put_blob = d.insert_ignore(self.blobs, BLOB_COLUMNS)
        p = d.params()
        self.get_blob = (
            f"SELECT {c('data')} FROM {self.blobs} "
            f"WHERE {self.by_thread(p)} AND {c('name')} = {p()}"
        )
        p = d.params()
        self.resolve_alias = (
            f"SELECT {self.names(('system', 'thread_id'))} "
            f"FROM {self.aliases} WHERE {c('alias')} = {p()}"
        )
        self.add_alias = d.insert_ignore(self.aliases, ALIAS_COLUMNS)
        p = d.params()
        self.find_receipt = (
            f"SELECT {self.names(('digest', 'receipt'))} FROM {self.receipts} "
            f"WHERE {self.by_thread(p)} AND {c('op_id')} = {p()}"
        )
        self.save_receipt = self.insert(self.receipts, RECEIPT_COLUMNS)
        p = d.params()
        self.prune_receipts = (
            f"DELETE FROM {self.receipts} WHERE {c('created_at')} < {p()}"
        )
        p = d.params()
        self.find_tombstone = (
            f"SELECT {self.names(('generation', 'lease_epoch'))} "
            f"FROM {self.tombstones} WHERE {self.by_thread(p)}"
        )
        self.save_tombstone = d.upsert(
            self.tombstones, TOMBSTONE_COLUMNS, ("system", "thread_id")
        )
        self.delete_thread_rows = tuple(
            f"DELETE FROM {d.table(name)} WHERE {self.by_thread(d.params())}"
            for name in THREAD_TABLES
        )
        self.delete_thread = (
            f"DELETE FROM {self.threads} WHERE {self.by_thread(d.params())}"
        )
