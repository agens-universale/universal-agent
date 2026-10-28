"""``SQLiteThreadStore`` — хранилище тредов v2 в одном файле SQLite.

Для одного воркера (или нескольких процессов на одной машине). Алгоритм
операций — общий SQL-движок (``gc_stores.sql.thread_store``); здесь файл,
диалект и схема: миграции ``sqlite/migrations`` применяются при открытии.

Время — часы машины (``clock``), формат ISO-8601 UTC фиксированной ширины.
Картинки и медиа ядра — вложения треда (``put_blob``) в той же базе.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from gc_core.stores import Stores

from gc_stores import migrations
from gc_stores.sql.agent_store import SqlAgentStore
from gc_stores.sql.thread_store import SqlThreadStore
from gc_stores.sqlite.database import SqliteDatabase
from gc_stores.sqlite.dialect import SqliteDialect

#: Миграции схемы SQLite (``sqlite/migrations``).
MIGRATIONS = migrations.load("gc_stores.sqlite")


class SQLiteThreadStore(SqlThreadStore):
    """Треды в файле ``path``; ``clock`` — часы хранилища (аренда,
    квитанции, штампы); ``database`` — общее подключение
    (``open_stores``)."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        database: SqliteDatabase | None = None,
    ) -> None:
        if (path is None) == (database is None):
            raise ValueError("нужен ровно один из path и database")
        if database is None:
            database = SqliteDatabase(path)
            database.ensure_sync(MIGRATIONS)
        self.path = database.path
        super().__init__(
            database,
            SqliteDialect(),
            migrations=MIGRATIONS,
            clock=clock or (lambda: datetime.now(UTC)),
        )
        self._checked = True  # схема применена при открытии базы


class SQLiteAgentStore(SqlAgentStore):
    """Хранилище агента ``agent_id`` в файле SQLite ``path`` (схема —
    при открытии) или на общем подключении ``database``."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        agent_id: str,
        database: SqliteDatabase | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (path is None) == (database is None):
            raise ValueError("нужен ровно один из path и database")
        if database is None:
            database = SqliteDatabase(path)
            database.ensure_sync(MIGRATIONS)
        super().__init__(
            database,
            SqliteDialect(),
            agent_id=agent_id,
            backend="sqlite",
            migrations=MIGRATIONS,
            clock=clock or (lambda: datetime.now(UTC)),
        )
        self._checked = True  # схема применена при открытии базы


def open_stores(
    path: str | Path,
    *,
    agent_id: str,
    clock: Callable[[], datetime] | None = None,
) -> Stores:
    """Треды и хранилище агента ``agent_id`` в одном файле SQLite (схема —
    при открытии)."""
    database = SqliteDatabase(path)
    database.ensure_sync(MIGRATIONS)
    return Stores(
        threads=SQLiteThreadStore(database=database, clock=clock),
        agent=SQLiteAgentStore(
            database=database, agent_id=agent_id, clock=clock
        ),
    )


__all__ = ["SQLiteAgentStore", "SQLiteThreadStore", "open_stores"]
