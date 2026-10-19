"""``MySQLThreadStore`` — хранилище тредов v2 на MySQL 8 (asyncmy).

Алгоритм операций — общий SQL-движок (``gc_stores.sql.thread_store``);
здесь подключение, диалект и схема (миграции ``mysql/migrations``: их
применяет ``ensure_schema()`` или ``gc-stores migrate``). Время — часы базы
(``UTC_TIMESTAMP(6)`` после блокировки строки).

Подключение::

    store = MySQLThreadStore("mysql://user:password@host:3306/agent")
    await store.ensure_schema()
    agent = await AsyncAgent.create(settings, thread_store=store)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from gc_stores import migrations
from gc_stores.mysql.database import MySQLDatabase
from gc_stores.mysql.dialect import MySQLDialect
from gc_stores.sql.thread_store import SqlThreadStore

#: Миграции схемы MySQL (``mysql/migrations``).
MIGRATIONS = migrations.load("gc_stores.mysql")


class MySQLThreadStore(SqlThreadStore):
    """Хранилище тредов в MySQL: ровно одно из ``dsn``, ``pool`` (пул
    asyncmy встраивающего) и ``database`` (общее подключение)."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: Any = None,
        database: MySQLDatabase | None = None,
        clock: Callable[[], datetime] | None = None,
        pool_options: dict[str, Any] | None = None,
    ) -> None:
        if database is None:
            database = MySQLDatabase(dsn, pool=pool, pool_options=pool_options)
        elif dsn is not None or pool is not None:
            raise ValueError("нужен ровно один из dsn, pool и database")
        super().__init__(
            database, MySQLDialect(), migrations=MIGRATIONS, clock=clock
        )


__all__ = ["MySQLThreadStore"]
