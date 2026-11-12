"""Подключение хранилищ на SQL-движке: драйвер по extra и привязка пула
к event loop."""

import asyncio
import builtins

import pytest
from gc_core.threads.models import ThreadKey
from gc_stores.postgres import PostgresThreadStore

KEY = ThreadKey(system="s", thread="t")


def _without(module: str, monkeypatch) -> None:
    real = builtins.__import__

    def fake(name, *args, **kwargs):
        if name == module or name.startswith(module + "."):
            raise ImportError(f"нет {module}")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_missing_postgres_driver_names_the_extra(monkeypatch):
    """Импорт и конструктор — без драйвера; первое подключение называет
    extra, который надо поставить."""
    _without("asyncpg", monkeypatch)
    store = PostgresThreadStore("postgresql://nobody@127.0.0.1:1/x")
    with pytest.raises(ImportError, match=r"gc-stores\[postgres\]"):
        asyncio.run(store.get_thread(KEY))


def test_pool_reopens_after_its_loop_closed(postgres_dsn, schemas):
    """``asyncio.run(ensure_schema())`` на старте, потом другой loop —
    пул прежнего loop брошен, открывается новый."""
    name = schemas.name()
    store = PostgresThreadStore(postgres_dsn, schema=name)
    asyncio.run(store.ensure_schema())
    assert asyncio.run(store.get_thread(KEY)) is None
    asyncio.run(store.close())


async def test_pool_in_another_live_loop_is_refused(postgres_dsn, pg_schema):
    store = PostgresThreadStore(postgres_dsn, schema=pg_schema)
    await store.get_thread(KEY)
    try:

        def other_loop():
            return asyncio.run(store.get_thread(KEY))

        with pytest.raises(RuntimeError, match="другом работающем"):
            await asyncio.to_thread(other_loop)
    finally:
        await store.close()
