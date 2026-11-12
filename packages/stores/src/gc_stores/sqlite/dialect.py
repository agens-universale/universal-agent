"""Диалект SQLite для SQL-движка: время — текст ISO-8601 UTC фиксированной
ширины (порядок строк = порядок моментов), блокировка — ``BEGIN IMMEDIATE``
на всю базу, поэтому без ``FOR UPDATE``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from gc_stores.sql.database import Conn, Database
from gc_stores.sql.dialect import Dialect, positional


def stamp(moment: datetime) -> str:
    """Время в базе: UTC, микросекунды всегда — строки сравниваются как
    моменты."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


class SqliteDialect(Dialect):
    name = "sqlite"
    lock_suffix = ""
    params = staticmethod(positional("?"))

    def time_in(self, moment: datetime) -> Any:
        return stamp(moment)

    def time_out(self, value: Any) -> datetime:
        return datetime.fromisoformat(value)

    async def now(self, conn: Conn) -> datetime:
        del conn
        return datetime.now(UTC)

    def insert_ignore(self, table: str, columns: Sequence[str]) -> str:
        marks = ", ".join("?" for _ in columns)
        names = ", ".join(self.ident(c) for c in columns)
        return f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({marks})"

    def upsert(
        self, table: str, columns: Sequence[str], keys: Sequence[str]
    ) -> str:
        names = ", ".join(self.ident(c) for c in columns)
        marks = ", ".join("?" for _ in columns)
        sets = ", ".join(
            f"{self.ident(c)} = excluded.{self.ident(c)}"
            for c in columns
            if c not in keys
        )
        conflict = ", ".join(self.ident(k) for k in keys)
        return (
            f"INSERT INTO {table} ({names}) VALUES ({marks}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {sets}"
        )

    async def table_exists(self, conn: Conn, name: str) -> bool:
        found = await conn.fetchval(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            name,
        )
        return found is not None

    @asynccontextmanager
    async def migration_session(self, db: Database) -> AsyncIterator[Conn]:
        async with db.transaction() as conn:
            yield conn
