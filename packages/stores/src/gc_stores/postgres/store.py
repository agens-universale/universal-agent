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

Схема (таблицы) — миграции ``postgres/migrations``: их применяет
``ensure_schema()`` или ``gc-stores migrate``; перед первой операцией
хранилище сверяет версию схемы (``gc_stores.migrations``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from gc_core.stores import Stores

from gc_stores import migrations
from gc_stores.postgres.database import PostgresDatabase
from gc_stores.postgres.dialect import PostgresDialect
from gc_stores.sql.agent_store import SqlAgentStore
from gc_stores.sql.thread_store import SqlThreadStore

#: Миграции схемы Postgres (``postgres/migrations``).
MIGRATIONS = migrations.load("gc_stores.postgres")


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
            database,
            PostgresDialect(database.schema),
            migrations=MIGRATIONS,
            clock=clock,
        )


class PostgresAgentStore(SqlAgentStore):
    """Хранилище агента ``agent_id`` в Postgres (промпты, AGENTS.md,
    скиллы, субагенты, память проекта); подключение — как у
    ``PostgresThreadStore``. Схема — те же миграции: ``ensure_schema()``
    или ``gc-stores migrate``."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        agent_id: str,
        pool: Any = None,
        database: PostgresDatabase | None = None,
        schema: str = "public",
        pool_options: dict[str, Any] | None = None,
    ) -> None:
        if database is None:
            database = PostgresDatabase(
                dsn, pool=pool, schema=schema, pool_options=pool_options
            )
        elif dsn is not None or pool is not None:
            raise ValueError("нужен ровно один из dsn, pool и database")
        super().__init__(
            database,
            PostgresDialect(database.schema),
            agent_id=agent_id,
            backend="postgres",
            migrations=MIGRATIONS,
        )


def open_stores(
    dsn: str,
    *,
    agent_id: str,
    schema: str = "public",
    clock: Callable[[], datetime] | None = None,
    pool_options: dict[str, Any] | None = None,
) -> Stores:
    """Треды и хранилище агента ``agent_id`` на одном пуле Postgres;
    ``await stores.aclose()`` закрывает пул."""
    database = PostgresDatabase(dsn, schema=schema, pool_options=pool_options)
    return Stores(
        threads=PostgresThreadStore(database=database, clock=clock),
        agent=PostgresAgentStore(database=database, agent_id=agent_id),
    )


__all__ = ["PostgresAgentStore", "PostgresThreadStore", "open_stores"]
