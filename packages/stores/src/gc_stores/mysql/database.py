"""Подключение к MySQL 8 (asyncmy): пул, транзакция READ COMMITTED.

DSN — ``mysql://user:password@host:3306/database``. Соединения пула —
в UTC (``time_zone = '+00:00'``), с autocommit: транзакцию открывает
``transaction()`` явно, с уровнем READ COMMITTED (у MySQL по умолчанию
REPEATABLE READ — с ним гонки давали бы ошибки вместо ``ThreadConflict``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from gc_stores.sql.database import Database, LoopBound

#: Сообщение с выводом тула бывает в мегабайты: клиент по умолчанию режет
#: пакет на 16 МБ, сервер — по своему ``max_allowed_packet``.
_PACKET = 64 * 1024 * 1024
_POOL_DEFAULTS = {"minsize": 1, "maxsize": 10}
#: Дедлок (1213) и ожидание блокировки (1205): транзакцию можно повторить.
_RETRYABLE = frozenset({1205, 1213})


def driver() -> Any:
    try:
        import asyncmy
        import asyncmy.cursors  # DictCursor — по атрибуту модуля
    except ImportError as exc:
        raise ImportError(
            "Хранилищам MySQL нужен драйвер asyncmy: установите gc-stores "
            "с extra — gc-stores[mysql] (docs/sdk/stores.md, раздел MySQL)"
        ) from exc
    return asyncmy


def parse_dsn(dsn: str) -> dict[str, Any]:
    """``mysql://user:pass@host:port/db`` → параметры ``create_pool``."""
    parts = urlsplit(dsn)
    if parts.scheme not in ("mysql", "mysql+asyncmy") or not parts.path.strip(
        "/"
    ):
        raise ValueError(
            "DSN MySQL — mysql://user:password@host:3306/database"
        )
    options = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    return {
        "host": parts.hostname or "127.0.0.1",
        "port": parts.port or 3306,
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "db": parts.path.strip("/"),
        **options,
    }


class _Conn:
    def __init__(self, cursor: Any) -> None:
        self._cur = cursor

    async def execute(self, sql: str, *args: Any) -> int:
        await self._cur.execute(sql, args or None)
        return max(self._cur.rowcount, 0)

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        await self._cur.executemany(sql, rows)

    async def fetchrow(self, sql: str, *args: Any) -> Mapping[str, Any] | None:
        await self._cur.execute(sql, args or None)
        return await self._cur.fetchone()

    async def fetch(self, sql: str, *args: Any) -> list[Mapping[str, Any]]:
        await self._cur.execute(sql, args or None)
        return list(await self._cur.fetchall())

    async def fetchval(self, sql: str, *args: Any) -> Any:
        found = await self.fetchrow(sql, *args)
        return None if found is None else next(iter(found.values()))


class MySQLDatabase(Database):
    """База MySQL: ровно один из ``dsn`` и ``pool`` (пул asyncmy с
    ``autocommit=True`` и UTC — его закрывает владелец)."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: Any = None,
        pool_options: dict[str, Any] | None = None,
    ) -> None:
        if (dsn is None) == (pool is None):
            raise ValueError("нужен ровно один из dsn и pool")
        self._options = (
            {
                **_POOL_DEFAULTS,
                "max_allowed_packet": _PACKET,
                **parse_dsn(dsn),
                **(pool_options or {}),
            }
            if dsn is not None
            else {}
        )
        self.name = self._options.get("db", "")
        self._pool = pool
        self._owns_pool = pool is None
        self._bound = LoopBound("MySQL-хранилище")

    async def pool(self) -> Any:
        if not self._owns_pool:
            return self._pool
        if self._bound.bind():
            self._pool = None
        if self._pool is None:
            async with self._bound.lock:
                if self._pool is None:
                    self._pool = await driver().create_pool(
                        autocommit=True,
                        charset="utf8mb4",
                        init_command="SET time_zone = '+00:00'",
                        **self._options,
                    )
        return self._pool

    @asynccontextmanager
    async def _cursor(self) -> AsyncIterator[Any]:
        pool = await self.pool()
        dict_cursor = driver().cursors.DictCursor
        async with pool.acquire() as conn, conn.cursor(dict_cursor) as cur:
            yield cur

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_Conn]:
        async with self._cursor() as cur:
            await cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            await cur.execute("START TRANSACTION")
            try:
                yield _Conn(cur)
            except BaseException:
                await cur.execute("ROLLBACK")
                raise
            await cur.execute("COMMIT")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[_Conn]:
        async with self._cursor() as cur:
            yield _Conn(cur)

    async def close(self) -> None:
        """Закрыть свой пул; чужой не трогается; повтор безвреден."""
        if not self._owns_pool or self._pool is None:
            return
        if self._bound.bind():
            self._pool = None
            return
        pool, self._pool = self._pool, None
        pool.close()
        await pool.wait_closed()

    def retryable(self, exc: BaseException) -> bool:
        code = exc.args[0] if exc.args else None
        return isinstance(code, int) and code in _RETRYABLE
