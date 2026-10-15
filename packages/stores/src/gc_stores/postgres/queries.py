"""SQL ``PostgresThreadStore``: запросы с именами таблиц нужной схемы.

Схема подставляется один раз при создании хранилища; параметры запросов —
только плейсхолдеры ``$n`` драйвера, данные в текст SQL не попадают.
"""

from __future__ import annotations

import re

from gc_stores.rows import THREAD_COLUMNS

_SCHEMA_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_BY_THREAD = "system = $1 AND thread_id = $2"
_ENTRY_COLUMNS = "seq, ts, data"
#: Порядок списка тредов — как ``gc_core.threads.rules.page_of``: строки
#: сравниваются по кодам символов (``COLLATE "C"``), а не по локали базы.
_ORDER = 'updated_at DESC, system COLLATE "C", thread_id COLLATE "C"'


def check_schema_name(name: str) -> str:
    """Имя схемы — идентификатор без кавычек; иначе ``ValueError``."""
    if not isinstance(name, str) or not _SCHEMA_NAME.fullmatch(name):
        raise ValueError(
            f"имя схемы {name!r}: латинские буквы, цифры и '_', "
            "не с цифры, до 63 символов"
        )
    return name


class Queries:
    """Тексты запросов для таблиц схемы ``schema``."""

    def __init__(self, schema: str) -> None:
        prefix = f'"{check_schema_name(schema)}".'
        self.schema_ident = prefix[:-1]
        threads = prefix + "gc_threads"
        entries = prefix + "gc_stream_entries"
        archives = prefix + "gc_archives"
        blobs = prefix + "gc_blobs"
        aliases = prefix + "gc_aliases"
        receipts = prefix + "gc_receipts"
        tombstones = prefix + "gc_tombstones"
        self._threads(threads)
        self._streams(entries, archives)
        self._side_tables(blobs, aliases, receipts, tombstones)
        self.delete_thread_rows = tuple(
            f"DELETE FROM {table} WHERE {_BY_THREAD}"
            for table in (entries, archives, blobs, aliases, receipts)
        )
        self.delete_thread = f"DELETE FROM {threads} WHERE {_BY_THREAD}"

    def _threads(self, threads: str) -> None:
        columns = ", ".join(("system", "thread_id", *THREAD_COLUMNS))
        marks = ", ".join(f"${n}" for n in range(1, len(THREAD_COLUMNS) + 3))
        sets = ", ".join(
            f"{name} = ${n}" for n, name in enumerate(THREAD_COLUMNS, start=3)
        )
        self.select_thread = (
            f"SELECT {columns} FROM {threads} WHERE {_BY_THREAD}"
        )
        self.lock_thread = self.select_thread + " FOR UPDATE"
        self.insert_thread = (
            f"INSERT INTO {threads} ({columns}) VALUES ({marks}) "
            "ON CONFLICT (system, thread_id) DO NOTHING RETURNING 1"
        )
        self.update_thread = f"UPDATE {threads} SET {sets} WHERE {_BY_THREAD}"
        self._threads_table = threads
        self._updates: dict[tuple[str, ...], str] = {}
        self.release = (
            f"UPDATE {threads} SET lease_owner = NULL, "
            f"lease_expires_at = NULL WHERE {_BY_THREAD} "
            "AND lease_owner = $3 AND lease_epoch = $4"
        )
        # Курсор — последняя запись прошлой страницы: (updated_at, system,
        # thread_id); дальше — всё, что идёт после неё в порядке _ORDER.
        self.list_threads = (
            f"SELECT {columns} FROM {threads} "
            "WHERE ($1::text IS NULL OR system = $1) "
            "AND ($2::text IS NULL OR workspace = $2) "
            "AND ($3::timestamptz IS NULL OR updated_at > $3) "
            "AND ($4::timestamptz IS NULL OR updated_at < $4) "
            "AND ($5::timestamptz IS NULL OR updated_at < $5 "
            'OR (updated_at = $5 AND (system COLLATE "C" > $6 '
            'OR (system = $6 AND thread_id COLLATE "C" > $7)))) '
            f"ORDER BY {_ORDER} LIMIT $8"
        )

    def update_columns(self, columns: tuple[str, ...]) -> str:
        """``UPDATE`` только этих колонок строки треда (``$3…`` — в порядке
        ``columns``): статус хода и продление аренды не переписывают
        состояние сессии."""
        statement = self._updates.get(columns)
        if statement is None:
            sets = ", ".join(
                f"{name} = ${n}" for n, name in enumerate(columns, start=3)
            )
            statement = (
                f"UPDATE {self._threads_table} SET {sets} WHERE {_BY_THREAD}"
            )
            self._updates[columns] = statement
        return statement

    def _streams(self, entries: str, archives: str) -> None:
        self.read_stream = (
            f"SELECT {_ENTRY_COLUMNS} FROM {entries} "
            f"WHERE {_BY_THREAD} AND stream = $3 AND seq > $4 "
            "ORDER BY seq LIMIT $5"
        )
        self.append_entry = (
            f"INSERT INTO {entries} "
            "(system, thread_id, stream, seq, ts, msg_key, data) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)"
        )
        self.clear_stream = (
            f"DELETE FROM {entries} WHERE {_BY_THREAD} AND stream = $3"
        )
        self.discard_entries = (
            f"DELETE FROM {entries} WHERE {_BY_THREAD} AND stream = $3 "
            "AND msg_key = ANY($4::text[])"
        )
        self.archive_stream = (
            f"INSERT INTO {archives} "
            "(system, thread_id, archive_id, seq, ts, data) "
            f"SELECT system, thread_id, $4::text, {_ENTRY_COLUMNS} "
            f"FROM {entries} "
            f"WHERE {_BY_THREAD} AND stream = $3"
        )
        self.read_archive = (
            f"SELECT {_ENTRY_COLUMNS} FROM {archives} "
            f"WHERE {_BY_THREAD} AND archive_id = $3 ORDER BY seq"
        )

    def _side_tables(
        self, blobs: str, aliases: str, receipts: str, tombstones: str
    ) -> None:
        self.put_blob = (
            f"INSERT INTO {blobs} "
            "(system, thread_id, name, mime, data, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (system, thread_id, name) DO NOTHING"
        )
        self.get_blob = (
            f"SELECT data FROM {blobs} WHERE {_BY_THREAD} AND name = $3"
        )
        self.resolve_alias = (
            f"SELECT system, thread_id FROM {aliases} WHERE alias = $1"
        )
        self.add_aliases = (
            f"INSERT INTO {aliases} (alias, system, thread_id) "
            "SELECT unnest($3::text[]), $1::text, $2::text "
            "ON CONFLICT (alias) DO NOTHING"
        )
        self.foreign_alias = (
            f"SELECT alias, system, thread_id FROM {aliases} "
            "WHERE alias = ANY($3::text[]) "
            "AND (system <> $1 OR thread_id <> $2) "
            "ORDER BY alias LIMIT 1"
        )
        self.find_receipt = (
            f"SELECT digest, receipt FROM {receipts} "
            f"WHERE {_BY_THREAD} AND op_id = $3"
        )
        self.save_receipt = (
            f"INSERT INTO {receipts} "
            "(system, thread_id, op_id, digest, receipt, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)"
        )
        self.prune_receipts = f"DELETE FROM {receipts} WHERE created_at < $1"
        self.find_tombstone = (
            f"SELECT generation, lease_epoch FROM {tombstones} "
            f"WHERE {_BY_THREAD}"
        )
        self.save_tombstone = (
            f"INSERT INTO {tombstones} "
            "(system, thread_id, generation, lease_epoch) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (system, thread_id) DO UPDATE SET "
            "generation = EXCLUDED.generation, "
            "lease_epoch = EXCLUDED.lease_epoch"
        )
