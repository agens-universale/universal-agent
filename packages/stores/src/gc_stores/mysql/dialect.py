"""Диалект MySQL 8 для SQL-движка.

Ключи — VARCHAR(255) с коллацией utf8mb4_0900_bin (порядок по кодам
символов и NO PAD задаёт схема, ``COLLATE`` в запросах не нужен). Время — DATETIME(6) в UTC
(наивное в базе, aware в коде). Строгий режим базы отверг бы длинный id
ошибкой драйвера — хранилище называет причину раньше (``check_ids``).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from gc_stores.sql.database import Conn, Database
from gc_stores.sql.dialect import Dialect, positional

#: Длина ключевых колонок (id треда, системы, алиас, op_id).
KEY_CHARS = 255
#: Ключ ресурса агента (agent_id, project_id, путь): первичный ключ InnoDB
#: — до 3072 байт, utf8mb4 — 4 байта на символ.
RESOURCE_LIMITS = {"agent_id": 128, "project_id": 128, "path": 400}
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

    async def execute_migration(self, conn: Conn, statement: str) -> None:
        # ADD COLUMN коммитится до записи версии миграции. Повтор после
        # обрыва допустим только при точном совместимом определении.
        match = re.fullmatch(
            r"ALTER TABLE (\w+) ADD COLUMN `?(\w+)`? (DATETIME\(6\)|VARCHAR\(32\)) NULL;?",
            " ".join(
                line.strip()
                for line in statement.splitlines()
                if not line.lstrip().startswith("--")
            ).strip(),
            re.IGNORECASE,
        )
        if match:
            table, column, kind = match.groups()
            row = await conn.fetchrow(
                "SELECT COLUMN_TYPE AS kind, IS_NULLABLE AS nullable "
                "FROM information_schema.columns WHERE table_schema = DATABASE() "
                "AND table_name = %s AND column_name = %s",
                table,
                column,
            )
            if row is not None:
                if (
                    row["kind"].lower() != kind.lower()
                    or row["nullable"] != "YES"
                ):
                    raise ValueError(
                        f"MySQL: несовместимое определение {table}.{column}"
                    )
                return
        await conn.execute(statement)

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

    def check_resource(
        self, *, agent_id: str = "", project_id: str = "", path: str = ""
    ) -> None:
        values = {"agent_id": agent_id, "project_id": project_id, "path": path}
        for name, value in values.items():
            limit = RESOURCE_LIMITS[name]
            if len(value) > limit:
                raise ValueError(
                    f"{name} длиннее {limit} символов ({len(value)}): "
                    "ограничение ключа MySQL-хранилища агента"
                )
