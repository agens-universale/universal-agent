"""Подключение к файлу SQLite (stdlib ``sqlite3``) для SQL-движка.

Драйвер синхронный, движок — асинхронный: каждая транзакция идёт в своём
потоке и своём соединении (все её запросы — в одном потоке), event loop
диск не ждёт. Запись — ``BEGIN IMMEDIATE``: база заперта на запись и между
процессами, ждать соседа — до 30 с. Файл базы создаётся с правами 0600:
в нём история и квитанции агента.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any, TypeVar

from gc_stores import migrations as schema
from gc_stores.sql.database import Database

T = TypeVar("T")
_JOURNALS = ("-wal", "-shm", "-journal")


def _private_file(path: str) -> None:
    """Создать файл базы с правами 0600 (``-wal``/``-shm`` SQLite создаёт
    с правами файла базы)."""
    if not Path(path).exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        path, timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    # FULL: запись треда переживает падение машины, а не только процесса.
    conn.execute("PRAGMA synchronous = FULL")
    return conn


class _Worker:
    """Один поток и одно соединение на транзакцию или чтение."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._conn: sqlite3.Connection | None = None

    def _run(self, work: Callable[[sqlite3.Connection], T]) -> T:
        if self._conn is None:
            self._conn = connect(self._path)
        return work(self._conn)

    async def call(self, work: Callable[[sqlite3.Connection], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._run, work)

    async def close(self) -> None:
        def shut(conn: sqlite3.Connection) -> None:
            conn.close()

        try:
            if self._conn is not None:
                await self.call(shut)
        finally:
            self._pool.shutdown(wait=False)


class _Conn:
    def __init__(self, worker: _Worker) -> None:
        self._w = worker

    async def execute(self, sql: str, *args: Any) -> int:
        return await self._w.call(lambda c: c.execute(sql, args).rowcount)

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        await self._w.call(lambda c: c.executemany(sql, rows))

    async def fetchrow(self, sql: str, *args: Any) -> Mapping[str, Any] | None:
        def work(c: sqlite3.Connection) -> dict | None:
            found = c.execute(sql, args).fetchone()
            return None if found is None else dict(found)

        return await self._w.call(work)

    async def fetch(self, sql: str, *args: Any) -> list[Mapping[str, Any]]:
        return await self._w.call(
            lambda c: [dict(r) for r in c.execute(sql, args).fetchall()]
        )

    async def fetchval(self, sql: str, *args: Any) -> Any:
        def work(c: sqlite3.Connection) -> Any:
            found = c.execute(sql, args).fetchone()
            return None if found is None else found[0]

        return await self._w.call(work)


class SqliteDatabase(Database):
    """База в файле ``path``; схема — миграции ``found`` при открытии."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        _private_file(self.path)
        with closing(connect(self.path)) as conn:
            # Режим журнала — свойство файла; вне транзакции.
            conn.execute("PRAGMA journal_mode = WAL")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_Conn]:
        worker = _Worker(self.path)
        try:
            await worker.call(lambda c: c.execute("BEGIN IMMEDIATE"))
            try:
                yield _Conn(worker)
            except BaseException:
                await worker.call(lambda c: c.execute("ROLLBACK"))
                raise
            await worker.call(lambda c: c.execute("COMMIT"))
        finally:
            await worker.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[_Conn]:
        worker = _Worker(self.path)
        try:
            yield _Conn(worker)
        finally:
            await worker.close()

    async def close(self) -> None:
        """Соединения живут одну операцию — закрывать нечего."""

    def protected_paths(self) -> list[str]:
        """Файл базы и её журналы: агенту к ним нельзя прикасаться."""
        return [self.path, *(f"{self.path}{s}" for s in _JOURNALS)]

    def ensure_sync(self, found: list[schema.Migration]) -> None:
        """Миграции при открытии (синхронно — как прежняя схема в
        конструкторе): воркер Temporal пишет в базу сразу."""
        code = schema.latest(found)
        table = schema.VERSION_TABLE
        with closing(connect(self.path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = _version(conn, table)
                if current > code:
                    raise schema.mismatch(current, code)
                if current < code:
                    _apply_sync(conn, table, found, current)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")


def _version(conn: sqlite3.Connection, table: str) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if exists is None:
        return 0
    found = conn.execute(f"SELECT version FROM {table}").fetchone()
    return int(found[0]) if found else 0


def _apply_sync(
    conn: sqlite3.Connection,
    table: str,
    found: list[schema.Migration],
    current: int,
) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {table} (version integer NOT NULL)"
    )
    for migration in found[current:]:
        for statement in migration.statements():
            conn.execute(statement)
        conn.execute(f"DELETE FROM {table}")
        conn.execute(
            f"INSERT INTO {table} (version) VALUES (?)", (migration.version,)
        )
