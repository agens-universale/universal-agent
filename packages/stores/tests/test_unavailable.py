"""Потеря соединения с базой — ``StoreUnavailable``: ход повторяет запись
с тем же ``op_id`` (квитанция делает повтор безопасным), а ошибки контракта
повтора не получают."""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from gc_core.threads.errors import StoreUnavailable, ThreadStoreError
from gc_core.threads.models import ThreadKey
from gc_stores.kv.memory import MemoryKV
from gc_stores.kv.thread_store import KvThreadStore
from gc_stores.sqlite.store import SQLiteThreadStore

KEY = ThreadKey(system="cli", thread="t")


def test_unavailable_is_a_store_error():
    assert issubclass(StoreUnavailable, ThreadStoreError)


async def test_sql_lost_connection_is_unavailable(tmp_path, monkeypatch):
    store = SQLiteThreadStore(str(tmp_path / "t.db"))
    await store.get_thread(KEY)

    @asynccontextmanager
    async def broken():
        raise ConnectionResetError("связь с базой потеряна")
        yield  # pragma: no cover

    monkeypatch.setattr(store._db, "connection", broken)
    monkeypatch.setattr(store._db, "transaction", broken)
    with pytest.raises(StoreUnavailable):
        await store.get_thread(KEY)
    with pytest.raises(StoreUnavailable):
        await store.prune_receipts(older_than=timedelta(0))
    await store.close()


async def test_kv_lost_connection_is_unavailable(monkeypatch):
    kv = MemoryKV()
    store = KvThreadStore(kv)

    async def broken(*_a, **_k):
        raise TimeoutError("таймаут соединения")

    monkeypatch.setattr(kv, "get", broken)
    with pytest.raises(StoreUnavailable):
        await store.get_thread(KEY)


def test_postgres_connection_errors_are_unavailable():
    asyncpg = pytest.importorskip("asyncpg")
    from gc_stores.postgres.database import PostgresDatabase

    db = PostgresDatabase("postgresql://u@localhost/db")
    assert db.unavailable(asyncpg.exceptions.ConnectionDoesNotExistError("x"))
    assert db.unavailable(ConnectionRefusedError())
    assert not db.unavailable(asyncpg.exceptions.UniqueViolationError("x"))


def test_mysql_connection_errors_are_unavailable():
    pytest.importorskip("asyncmy")
    from gc_stores.mysql.database import MySQLDatabase, driver

    db = MySQLDatabase("mysql://u@localhost/db")
    gone = driver().errors.OperationalError(2006, "MySQL server has gone away")
    assert db.unavailable(gone)
    assert not db.unavailable(driver().errors.OperationalError(1213, "lock"))


def test_redis_connection_errors_are_unavailable():
    redis = pytest.importorskip("redis")
    from gc_stores.redis.kv import RedisKV

    kv = RedisKV("redis://localhost:6379/0")
    assert kv.unavailable(redis.exceptions.ConnectionError("x"))
    assert kv.unavailable(redis.exceptions.TimeoutError("x"))
    assert not kv.unavailable(redis.exceptions.ResponseError("x"))


def test_s3_connection_errors_are_unavailable():
    errors = pytest.importorskip("botocore.exceptions")
    from gc_stores.s3.kv import S3KV

    kv = S3KV(bucket="b")
    lost = errors.EndpointConnectionError(endpoint_url="http://x")
    assert kv.unavailable(lost)
    assert not kv.unavailable(ValueError("x"))


async def test_unavailable_message_does_not_leak_driver_text(
    tmp_path, monkeypatch
):
    """Текст ошибки драйвера (хост, пользователь, кусок DSN) не попадает в
    сообщение: оно уходит в статус задачи и колбэк. Исходная ошибка — только
    в цепочке исключения, для логов."""
    secret = "postgresql://admin:hunter2@db.internal:5432/gc"
    store = SQLiteThreadStore(str(tmp_path / "t.db"))
    await store.get_thread(KEY)

    @asynccontextmanager
    async def broken():
        raise ConnectionRefusedError(f"connect to {secret} refused")
        yield  # pragma: no cover

    monkeypatch.setattr(store._db, "connection", broken)
    with pytest.raises(StoreUnavailable) as err:
        await store.get_thread(KEY)
    assert "hunter2" not in str(err.value)
    assert "db.internal" not in str(err.value)
    assert "ConnectionRefusedError" in str(err.value)
    assert isinstance(err.value.__cause__, ConnectionRefusedError)

    kv = MemoryKV()
    kv_store = KvThreadStore(kv)

    async def kv_broken(*_a, **_k):
        raise TimeoutError(f"timeout to {secret}")

    monkeypatch.setattr(kv, "get", kv_broken)
    with pytest.raises(StoreUnavailable) as err:
        await kv_store.get_thread(KEY)
    assert "hunter2" not in str(err.value)
    await store.close()
