"""Диалект Postgres для SQL-движка."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from gc_stores.sql.database import Conn
from gc_stores.sql.dialect import Dialect, numbered


class PostgresDialect(Dialect):
    name = "postgres"
    #: Порядок строк — по кодам символов, а не по локали базы.
    collate = ' COLLATE "C"'
    params = staticmethod(numbered("${}"))

    def __init__(self, schema: str) -> None:
        super().__init__(f'"{schema}".')

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
