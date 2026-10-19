"""``PostgresThreadStore`` — хранилище тредов v2 на Postgres (asyncpg).

Алгоритм операций — общий SQL-движок (``gc_stores.sql.thread_store``);
здесь подключение и диалект Postgres. Время — часы базы
(``clock_timestamp()`` после блокировки строки): истечение аренды не
зависит от часов инстансов.

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

import hashlib
from collections.abc import Callable
from datetime import datetime
from importlib import resources
from typing import Any

from gc_stores.postgres.database import PostgresDatabase
from gc_stores.postgres.dialect import PostgresDialect
from gc_stores.rows import TABLES
from gc_stores.sql.thread_store import SqlThreadStore


def _schema_lock_id(schema: str) -> int:
    """Номер advisory-блокировки создания схемы: два инстанса, стартующие
    одновременно, не создают таблицы наперегонки."""
    digest = hashlib.sha256(f"gc-stores:{schema}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class PostgresThreadStore(SqlThreadStore):
    """Хранилище тредов в Postgres.

    ``dsn`` — строка подключения: пул создаёт и закрывает само хранилище;
    ``pool`` — готовый пул встраивающего (закрывает его владелец);
    ``database`` — общее подключение (``open_stores``). Ровно одно из трёх.
    ``schema`` — схема базы, где лежат таблицы.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: Any = None,
        database: PostgresDatabase | None = None,
        schema: str = "public",
        clock: Callable[[], datetime] | None = None,
        pool_options: dict[str, Any] | None = None,
    ) -> None:
        if database is None:
            database = PostgresDatabase(
                dsn, pool=pool, schema=schema, pool_options=pool_options
            )
        elif dsn is not None or pool is not None:
            raise ValueError("нужен ровно один из dsn, pool и database")
        self._database = database
        super().__init__(
            database, PostgresDialect(database.schema), clock=clock
        )

    async def ensure_schema(self) -> None:
        """Создать схему и таблицы, если их нет; повторный вызов безвреден.

        Всё на месте — DDL не выполняется: Postgres проверяет право
        ``CREATE`` раньше, чем ``IF NOT EXISTS``, и пользователь приложения
        без этого права (таблицы создал администратор) иначе упал бы.
        """
        schema = self._database.schema
        ddl = (
            resources.files("gc_stores.postgres")
            .joinpath("schema.sql")
            .read_text(encoding="utf-8")
        )
        async with self._database.transaction() as conn:
            present = await conn.fetchval(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = $1 AND tablename = ANY($2::text[])",
                schema,
                list(TABLES),
            )
            if present == len(TABLES):
                return
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", _schema_lock_id(schema)
            )
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_namespace WHERE nspname = $1", schema
            )
            if not exists:
                await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'SET LOCAL search_path TO "{schema}"')
            await conn.execute(ddl)


__all__ = ["PostgresThreadStore"]
