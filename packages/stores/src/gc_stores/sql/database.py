"""Подключение SQL-движка к базе: соединение (``Conn``), транзакция и пул.

Драйверы разные (asyncpg, asyncmy, stdlib ``sqlite3``), движку нужен один
узкий интерфейс: выполнить запрос, получить строки как словари, открыть
транзакцию. Параметры запросов — позиционные, по порядку плейсхолдеров.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, TypeVar

from gc_core.threads.errors import StoreUnavailable

T = TypeVar("T")

#: Сколько раз повторить транзакцию, которую база отменила из-за дедлока
#: или конфликта сериализации: работа повторяется целиком.
ATTEMPTS = 3


class Conn(Protocol):
    """Соединение внутри транзакции или вне её."""

    async def execute(self, sql: str, *args: Any) -> int:
        """Выполнить; вернуть число затронутых строк."""

    async def executemany(self, sql: str, rows: list[tuple]) -> None: ...

    async def fetchrow(
        self, sql: str, *args: Any
    ) -> Mapping[str, Any] | None: ...

    async def fetch(self, sql: str, *args: Any) -> list[Mapping[str, Any]]: ...

    async def fetchval(self, sql: str, *args: Any) -> Any: ...


class Database(ABC):
    """База хранилища: пул соединений или файл.

    Подключение ленивое — на loop первого вызова. ``close()`` идемпотентен:
    его зовут оба хранилища общего подключения (``open_stores``).
    """

    @abstractmethod
    def transaction(self) -> AbstractAsyncContextManager[Conn]:
        """Транзакция записи: READ COMMITTED (Postgres, MySQL) или
        ``BEGIN IMMEDIATE`` (SQLite)."""

    @abstractmethod
    def connection(self) -> AbstractAsyncContextManager[Conn]:
        """Соединение для чтения, без транзакции."""

    @abstractmethod
    async def close(self) -> None: ...

    def retryable(self, exc: BaseException) -> bool:
        """База отменила транзакцию из-за дедлока или сериализации —
        работу можно повторить целиком."""
        del exc
        return False

    def unavailable(self, exc: BaseException) -> bool:
        """Соединения с базой нет или оно оборвалось — запись повторит
        вызывающий (``StoreUnavailable``)."""
        return isinstance(exc, (ConnectionError, TimeoutError))

    def protected_paths(self) -> list[str]:
        """Файлы базы на диске машины (SQLite); у сетевых — нет."""
        return []


async def in_transaction(
    db: Database,
    work: Callable[[Conn], Awaitable[T]],
    *,
    attempts: int = ATTEMPTS,
) -> T:
    """``work`` в транзакции; дедлок или сериализация — повтор целиком,
    обрыв соединения — ``StoreUnavailable``."""
    for attempt in range(attempts):
        try:
            async with db.transaction() as conn:
                return await work(conn)
        except Exception as exc:
            if db.unavailable(exc):
                raise StoreUnavailable.from_error(exc) from exc
            if attempt + 1 >= attempts or not db.retryable(exc):
                raise
    raise AssertionError("unreachable")


async def guarded(db: Database, call: Awaitable[T]) -> T:
    """Вызов к базе вне ``in_transaction``/``read_on`` (сверка схемы):
    обрыв соединения — ``StoreUnavailable``."""
    try:
        return await call
    except Exception as exc:
        if db.unavailable(exc):
            raise StoreUnavailable.from_error(exc) from exc
        raise


async def read_on(db: Database, work: Callable[[Conn], Awaitable[T]]) -> T:
    """``work`` на соединении без транзакции; обрыв — ``StoreUnavailable``."""
    try:
        async with db.connection() as conn:
            return await work(conn)
    except Exception as exc:
        if db.unavailable(exc):
            raise StoreUnavailable.from_error(exc) from exc
        raise


class LoopBound:
    """Свой пул драйвера привязан к loop, где открыт.

    Прежний loop закрыт (частое: ``asyncio.run(store.ensure_schema())`` на
    старте, потом loop агента) — пул брошен и откроется заново; живой чужой
    loop — понятный ``RuntimeError``, а не зависание внутри драйвера.
    """

    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._loop: asyncio.AbstractEventLoop | None = None
        self.lock: asyncio.Lock | None = None

    def bind(self) -> bool:
        """Привязать к текущему loop; ``True`` — пул надо открыть заново."""
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return False
        if self._loop is not None and not self._loop.is_closed():
            raise RuntimeError(
                f"{self._owner}: пул открыт в другом работающем event loop; "
                "хранилище со своим пулом живёт в одном loop"
            )
        self._loop = loop
        self.lock = asyncio.Lock()
        return True

    def current(self) -> bool:
        """Пул открыт в текущем loop."""
        try:
            return self._loop is asyncio.get_running_loop()
        except RuntimeError:
            return False
