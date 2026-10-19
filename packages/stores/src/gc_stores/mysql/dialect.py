"""Диалект MySQL 8 для SQL-движка.

Ключи — VARCHAR(255) с коллацией utf8mb4_bin (порядок по кодам символов
задаёт схема, ``COLLATE`` в запросах не нужен). Время — DATETIME(6) в UTC
(наивное в базе, aware в коде). Строгий режим базы отверг бы длинный id
ошибкой драйвера — хранилище называет причину раньше (``check_ids``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from gc_stores.sql.database import Conn, Database
from gc_stores.sql.dialect import Dialect, positional

#: Длина ключевых колонок (id треда, системы, алиас, op_id).
KEY_CHARS = 255
_LOCK_SECONDS = 60


class MySQLDialect(Dialect):
    name = "mysql"
    params = staticmethod(positional("%s"))

    def ident(self, name: str) -> str:
        return f"`{name}`"

    def time_in(self, moment: datetime) -> Any:
        return moment.astimezone(UTC).replace(tzinfo=None)

    def time_out(self, value: Any) -> datetime:
        return value.replace(tzinfo=UTC)

    async def now(self, conn: Conn) -> datetime:
        return self.time_out(await conn.fetchval("SELECT UTC_TIMESTAMP(6)"))

    def _insert(self, table: str, columns: Sequence[str]) -> str:
        names = ", ".join(self.ident(c) for c in columns)
        marks = ", ".join("%s" for _ in columns)
        return f"INSERT INTO {table} ({names}) VALUES ({marks})"

    def insert_ignore(self, table: str, columns: Sequence[str]) -> str:
        # Не INSERT IGNORE: он гасит и ошибки строгого режима (обрезка
        # длинной строки прошла бы молча). Пустое обновление — 0 строк.
        first = self.ident(columns[0])
        return (
            f"{self._insert(table, columns)} "
            f"ON DUPLICATE KEY UPDATE {first} = {first}"
        )

    def upsert(
        self, table: str, columns: Sequence[str], keys: Sequence[str]
    ) -> str:
        sets = ", ".join(
            f"{self.ident(c)} = new.{self.ident(c)}"
            for c in columns
            if c not in keys
        )
        return (
            f"{self._insert(table, columns)} AS new "
            f"ON DUPLICATE KEY UPDATE {sets}"
        )

    async def table_exists(self, conn: Conn, name: str) -> bool:
        found = await conn.fetchval(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            name,
        )
        return found is not None

    @asynccontextmanager
    async def migration_session(self, db: Database) -> AsyncIterator[Conn]:
        """DDL MySQL не транзакционный: миграции — в соединении под
        именованной блокировкой базы (``GET_LOCK``), версия — после
        каждого файла."""
        async with db.connection() as conn:
            name = f"gc-stores:{await conn.fetchval('SELECT DATABASE()')}"
            got = await conn.fetchval(
                "SELECT GET_LOCK(%s, %s)", name, _LOCK_SECONDS
            )
            if got != 1:
                raise TimeoutError(
                    f"MySQL: блокировка миграций {name} занята дольше "
                    f"{_LOCK_SECONDS} с"
                )
            try:
                yield conn
            finally:
                await conn.fetchval("SELECT RELEASE_LOCK(%s)", name)

    def check_ids(self, *values: str) -> None:
        for value in values:
            if len(value) > KEY_CHARS:
                raise ValueError(
                    f"ключ длиннее {KEY_CHARS} символов ({len(value)}): "
                    "в MySQL-хранилище id треда, системы, алиас и op_id — "
                    f"до {KEY_CHARS} символов"
                )
