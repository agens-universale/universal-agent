"""``SQLiteThreadStore`` — хранилище тредов v2 в одном файле SQLite.

Для одного воркера (или нескольких процессов на одной машине). Алгоритм
операций — общий SQL-движок (``gc_stores.sql.thread_store``); здесь файл,
диалект и схема: миграции ``sqlite/migrations`` применяются при открытии.

Время — часы машины (``clock``), формат ISO-8601 UTC фиксированной ширины.
Файлы треда (``write_thread_file``/``read_thread_file``) — в той же базе:
картинки и медиа ядра под Temporal живут с тредом, а не на диске воркера.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from gc_core.stores import Stores
from gc_core.thread_files import check_name
from gc_core.threads.models import ThreadKey

from gc_stores import migrations
from gc_stores.sql.agent_store import SqlAgentStore
from gc_stores.sql.thread_store import SqlThreadStore
from gc_stores.sqlite.database import SqliteDatabase, connect
from gc_stores.sqlite.dialect import SqliteDialect, stamp

#: Миграции схемы SQLite (``sqlite/migrations``).
MIGRATIONS = migrations.load("gc_stores.sqlite")
_BY_THREAD = "system = ? AND thread_id = ? AND name = ?"
_FILE_MIME = "application/octet-stream"


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

    def _connect(self) -> sqlite3.Connection:
        return connect(self.path)

    # --- файлы треда (картинки и медиа ядра) -------------------------------

    def write_thread_file(
        self, system_id: str, thread_id: str, name: str, data: bytes
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gc_blobs "
                "(system, thread_id, name, mime, data, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    system_id,
                    thread_id,
                    check_name(name),
                    _FILE_MIME,
                    data,
                    stamp(self._clock()),
                ),
            )

    def read_thread_file(
        self, system_id: str, thread_id: str, name: str
    ) -> bytes | None:
        key = ThreadKey(system=system_id, thread=thread_id)
        with closing(self._connect()) as conn:
            found = conn.execute(
                f"SELECT data FROM gc_blobs WHERE {_BY_THREAD}",
                (key.system, key.thread, check_name(name)),
            ).fetchone()
        return None if found is None else bytes(found[0])


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
