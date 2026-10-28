"""Хранилище агента на SQL (Postgres, MySQL, SQLite): конформанс
``AgentStore``, разделение агентов и проектов, метка версии промптов по
умолчанию."""

import logging

import pytest
from gc_core.agent_store import ProjectKey
from gc_testkit.agent_store_conformance import (
    PROJECT,
    assert_conforms,
    seed_store,
)

BACKENDS = ["sqlite", "postgres", "mysql"]


class Factory:
    """Хранилища агента одной базы (``same=True``) или свежей."""

    def __init__(self, request, tmp_path, backend):
        self.request, self.tmp_path, self.backend = request, tmp_path, backend
        self.made = []
        self._last = None
        self._count = 0

    async def _database(self):
        self._count += 1
        if self.backend == "sqlite":
            return self.tmp_path / f"a{self._count}.db"
        if self.backend == "postgres":
            dsn = self.request.getfixturevalue("postgres_dsn")
            schema = await self.request.getfixturevalue("schemas").acreate()
            return (dsn, schema)
        return await self.request.getfixturevalue("mysql_databases").acreate()

    def _open(self, where, agent_id):
        if self.backend == "sqlite":
            from gc_stores.sqlite import SQLiteAgentStore

            return SQLiteAgentStore(where, agent_id=agent_id)
        if self.backend == "postgres":
            from gc_stores.postgres import PostgresAgentStore

            dsn, schema = where
            return PostgresAgentStore(dsn, schema=schema, agent_id=agent_id)
        from gc_stores.mysql import MySQLAgentStore

        return MySQLAgentStore(where, agent_id=agent_id)

    async def __call__(self, agent_id="a", *, same=False):
        if not same or self._last is None:
            self._last = await self._database()
        store = self._open(self._last, agent_id)
        self.made.append(store)
        return store


@pytest.fixture(params=BACKENDS)
async def stores(request, tmp_path):
    factory = Factory(request, tmp_path, request.param)
    yield factory
    for store in factory.made:
        await store.close()


async def test_sql_agent_store_conforms(stores):
    async def make(seed):
        store = await stores()
        await seed_store(store, seed, project=PROJECT)
        return store

    await assert_conforms(make)


async def test_two_agents_do_not_see_each_other(stores):
    a = await stores("a")
    b = await stores("b", same=True)
    await a.put_resource("prompt", "system", b"A")
    await a.write_memory(PROJECT, "память a", expected_version=None)
    assert await b.read("prompt", "system") is None
    assert await b.read_memory(PROJECT) is None


async def test_project_resources_are_per_project(stores):
    store = await stores()
    await store.put_resource("instructions", "L3", b"P1", project="p1")
    assert (
        await store.read("instructions", "L3", project=ProjectKey("p1"))
        == b"P1"
    )
    assert (
        await store.read("instructions", "L3", project=ProjectKey("p2"))
        is None
    )


async def test_stale_bundled_prompt_warns_once(stores, caplog):
    store = await stores()
    await store.put_resource("prompt", "system", b"S", source="bundled@0.0.1")
    caplog.set_level(logging.WARNING)
    await store.read("prompt", "system")
    await store.read("prompt", "system")
    hints = [r for r in caplog.records if "--with-defaults" in r.getMessage()]
    assert len(hints) == 1
    assert "0.0.1" in hints[0].getMessage()


async def test_fresh_bundled_prompt_does_not_warn(stores, caplog):
    from gc_stores.defaults import bundled_source

    store = await stores()
    await store.put_resource("prompt", "system", b"S", source=bundled_source())
    caplog.set_level(logging.WARNING)
    await store.read("prompt", "system")
    assert not [
        r for r in caplog.records if "--with-defaults" in r.getMessage()
    ]


async def test_label_names_agent_and_the_fix(stores):
    store = await stores("coder")
    label = store.label("prompt", "system")
    assert "coder" in label and "--with-defaults" in label


async def test_list_resources_and_delete(stores):
    store = await stores()
    await store.put_resource("prompt", "system", b"S")
    await store.put_resource("skill", "project/x/SKILL.md", b"X", project="p")
    listed = {
        (r.kind, r.name, r.project)
        for r in await store.list_resources(project="p")
    }
    assert listed == {
        ("prompt", "system", None),
        ("skill", "project/x/SKILL.md", "p"),
    }
    await store.delete_resource("prompt", "system")
    assert await store.read("prompt", "system") is None


async def test_list_of_a_file_is_not_a_directory(stores):
    store = await stores()
    await store.put_resource("skill", "user/s/SKILL.md", b"S")
    with pytest.raises(NotADirectoryError):
        await store.list("skill", "user/s/SKILL.md")
    with pytest.raises(FileNotFoundError):
        await store.list("skill", "user/none")
