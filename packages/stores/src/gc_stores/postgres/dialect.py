"""Диалект Postgres для SQL-движка."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from gc_stores.sql.database import Conn, Database
from gc_stores.sql.dialect import Dialect, numbered


def _schema_lock_id(schema: str) -> int:
    """Номер advisory-блокировки миграций схемы: два инстанса, стартующие
    одновременно, не применяют миграцию наперегонки."""
    digest = hashlib.sha256(f"gc-stores:{schema}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class PostgresDialect(Dialect):
    name = "postgres"
    #: Порядок строк — по кодам символов, а не по локали базы.
    collate = ' COLLATE "C"'
    params = staticmethod(numbered("${}"))

    def __init__(self, schema: str) -> None:
        super().__init__(f'"{schema}".')
        self.schema = schema

    async def table_exists(self, conn: Conn, name: str) -> bool:
        return bool(
            await conn.fetchval(
                "SELECT to_regclass($1::text) IS NOT NULL",
                f'"{self.schema}".{name}',
            )
        )

    @asynccontextmanager
    async def migration_session(self, db: Database) -> AsyncIterator[Conn]:
        """Транзакция миграций (DDL Postgres транзакционный) под
        advisory-блокировкой схемы; схема создана, ``search_path`` — она."""
        async with db.transaction() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)",
                _schema_lock_id(self.schema),
            )
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_namespace WHERE nspname = $1", self.schema
            )
            if not exists:
                await conn.execute(f'CREATE SCHEMA "{self.schema}"')
            await conn.execute(f'SET LOCAL search_path TO "{self.schema}"')
            yield conn

    def cast_text(self, placeholder: str) -> str:
        return f"{placeholder}::text"

    def time_in(self, moment: datetime) -> Any:
        return moment

    def time_out(self, value: Any) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def now(self, conn: Conn) -> datetime:
        """Часы базы в момент вызова (не начала транзакции): запись,
        ждавшая блокировку, иначе получила бы время раньше той, что её
        держала."""
        return self.time_out(await conn.fetchval("SELECT clock_timestamp()"))

    def insert_ignore(self, table: str, columns: Sequence[str]) -> str:
        marks = self.params()
        return (
            f"INSERT INTO {table} ({', '.join(self.ident(c) for c in columns)}) "
            f"VALUES ({', '.join(marks() for _ in columns)}) "
            "ON CONFLICT DO NOTHING"
        )

    def upsert(
        self, table: str, columns: Sequence[str], keys: Sequence[str]
    ) -> str:
        marks = self.params()
        sets = ", ".join(
            f"{self.ident(c)} = EXCLUDED.{self.ident(c)}"
            for c in columns
            if c not in keys
        )
        return (
            f"INSERT INTO {table} ({', '.join(self.ident(c) for c in columns)}) "
            f"VALUES ({', '.join(marks() for _ in columns)}) "
            f"ON CONFLICT ({', '.join(self.ident(k) for k in keys)}) "
            f"DO UPDATE SET {sets}"
        )
