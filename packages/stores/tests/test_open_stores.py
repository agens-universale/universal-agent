"""``open_stores``: хранилище тредов и хранилище агента на одном
подключении (пуле или файле); ``Stores.aclose()`` закрывает его один раз."""

import pytest
from gc_core.stores import Stores
from gc_core.threads.models import LeaseRequest, ThreadKey

KEY = ThreadKey(system="s", thread="t")


@pytest.fixture(params=["sqlite", "postgres", "mysql"])
async def stores(request, tmp_path):
    if request.param == "sqlite":
        from gc_stores import sqlite

        made = sqlite.open_stores(tmp_path / "s.db", agent_id="a")
    elif request.param == "postgres":
        from gc_stores import postgres

        dsn = request.getfixturevalue("postgres_dsn")
        schema = request.getfixturevalue("schemas").name()
        made = postgres.open_stores(dsn, agent_id="a", schema=schema)
        await made.threads.ensure_schema()
    else:
        from gc_stores import mysql

        databases = request.getfixturevalue("mysql_databases")
        made = mysql.open_stores(
            await databases.acreate(migrate=False), agent_id="a"
        )
        await made.threads.ensure_schema()
    yield made
    await made.aclose()


async def test_open_stores_share_one_connection(stores):
    assert isinstance(stores, Stores)
    assert stores.threads._db is stores.agent._db
    await stores.agent.put_resource("prompt", "system", b"S")
    assert await stores.agent.read("prompt", "system") == b"S"
    record = await stores.threads.claim(KEY, LeaseRequest(owner="w"))
    assert record.lease is not None


async def test_closed_stores_reopen_on_next_call(stores):
    await stores.aclose()
    await stores.aclose()  # повтор безвреден
    assert await stores.agent.read("prompt", "system") is None
