"""Подключение к Postgres (asyncpg): пул, транзакция READ COMMITTED.

Пул создаёт и закрывает сама база хранилища (лениво, в loop первого
вызова; ``pool_options`` — в ``asyncpg.create_pool``) либо его отдаёт
встраивающий (``pool=``) — тогда закрывает владелец.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from gc_stores.sql.database import Database, LoopBound

_POOL_DEFAULTS = {"min_size": 1, "max_size": 10}
#: Сессия, замёрзшая посреди транзакции (инстанс остановлен), держала бы
#: блокировку строки треда бессрочно — и чужой claim не перехватил бы тред
#: даже после истечения аренды. Транзакции хранилища — миллисекунды.
_SERVER_DEFAULTS = {"idle_in_transaction_session_timeout": "60s"}
_SCHEMA_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_RETRYABLE = frozenset({"DeadlockDetectedError", "SerializationError"})


def check_schema_name(name: str) -> str:
    """Имя схемы — идентификатор без кавычек; иначе ``ValueError``."""
    if not isinstance(name, str) or not _SCHEMA_NAME.fullmatch(name):
        raise ValueError(
            f"имя схемы {name!r}: латинские буквы, цифры и '_', "
            "не с цифры, до 63 символов"
        )
    return name


def driver() -> Any:
    try:
        import asyncpg
    except ImportError as exc:
        raise ImportError(
            "Хранилищам Postgres нужен драйвер asyncpg: установите "
            "gc-stores с extra — gc-stores[postgres] "
            "(docs/sdk/stores.md, раздел Postgres)"
        ) from exc
    return asyncpg


def _affected(status: str) -> int:
    """Число строк из статуса команды (``DELETE 3``, ``INSERT 0 1``)."""
    tail = status.rsplit(" ", 1)[-1] if status else ""
    return int(tail) if tail.isdigit() else 0


class _Conn:
    def __init__(self, conn: Any) -> None:
        self._c = conn

    async def execute(self, sql: str, *args: Any) -> int:
        return _affected(await self._c.execute(sql, *args))

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        await self._c.executemany(sql, rows)

    async def fetchrow(self, sql: str, *args: Any) -> Mapping[str, Any] | None:
        found = await self._c.fetchrow(sql, *args)
        return None if found is None else dict(found)

    async def fetch(self, sql: str, *args: Any) -> list[Mapping[str, Any]]:
        return [dict(r) for r in await self._c.fetch(sql, *args)]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._c.fetchval(sql, *args)


class PostgresDatabase(Database):
    """База Postgres: ровно один из ``dsn`` и ``pool``; ``schema`` — где
    лежат таблицы хранилищ."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: Any = None,
        schema: str = "public",
        pool_options: dict[str, Any] | None = None,
    ) -> None:
        if (dsn is None) == (pool is None):
            raise ValueError("нужен ровно один из dsn и pool")
        self.schema = check_schema_name(schema)
        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None
        options = {**_POOL_DEFAULTS, **(pool_options or {})}
        options["server_settings"] = {
            **_SERVER_DEFAULTS,
            **options.get("server_settings", {}),
        }
        self._options = options
        self._bound = LoopBound("Postgres-хранилище")

    async def pool(self) -> Any:
        if not self._owns_pool:
            return self._pool
        if self._bound.bind():
            self._pool = None
        if self._pool is None:
            async with self._bound.lock:
                if self._pool is None:
                    self._pool = await driver().create_pool(
                        self._dsn, **self._options
                    )
        return self._pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_Conn]:
        pool = await self.pool()
        # Явно: контракт держится на «FOR UPDATE → свежий снимок» READ
        # COMMITTED; с default_transaction_isolation = repeatable read на
        # сервере обычные гонки давали бы SerializationError вместо
        # ThreadConflict или той же квитанции.
        async with (
            pool.acquire() as conn,
            conn.transaction(isolation="read_committed"),
        ):
            yield _Conn(conn)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[_Conn]:
        pool = await self.pool()
        async with pool.acquire() as conn:
            yield _Conn(conn)

    async def close(self) -> None:
        """Закрыть свой пул (следующий вызов откроет новый); чужой
        (``pool=``) не трогается; повторный вызов безвреден."""
        if not self._owns_pool or self._pool is None:
            return
        if self._bound.bind():
            # Пул прежнего (закрытого) loop закрыть уже нечем — брошен.
            self._pool = None
            return
        pool, self._pool = self._pool, None
        await pool.close()

    def retryable(self, exc: BaseException) -> bool:
        return type(exc).__name__ in _RETRYABLE

    def unavailable(self, exc: BaseException) -> bool:
        """Сеть до базы (``OSError``: отказ, DNS, таймаут) или соединение,
        которое драйвер счёл потерянным."""
        if isinstance(exc, (OSError, TimeoutError)):
            return True
        try:
            errors = driver().exceptions
        except ImportError:  # драйвера нет — это не обрыв соединения
            return False
        return isinstance(
            exc, (errors.PostgresConnectionError, errors.InterfaceError)
        )
