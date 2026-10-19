"""Сочетания хранилищ в SDK: треды и ресурсы агента — в разных местах.

«Агент на файлах, треды в базе» и «агент в базе, треды на файлах» — ход
проходит, тред лежит там, где хранилище тредов, промпт берётся там, где
хранилище агента."""

import pytest
from gc_core.agent_store import FileAgentStore, Prompt
from gc_core.aio import AsyncAgent
from gc_core.stores import Stores
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_stores.sync import copy_defaults
from scripted_model import scripted_settings

KEY = ThreadKey(system="cli", thread="demo")
OWN = "Системный промпт из хранилища агента в базе."


@pytest.fixture(params=["sqlite", "postgres"])
async def database(request, tmp_path):
    """Фабрика хранилищ одной базы: ``threads()`` и ``agent()``."""
    if request.param == "sqlite":
        from gc_stores import sqlite

        made = sqlite.open_stores(tmp_path / "db.sqlite", agent_id="a")
    else:
        from gc_stores import postgres

        dsn = request.getfixturevalue("postgres_dsn")
        schema = request.getfixturevalue("schemas").name()
        made = postgres.open_stores(dsn, agent_id="a", schema=schema)
        await made.threads.ensure_schema()
    yield made
    await made.aclose()


async def _run(settings, stores):
    async with await AsyncAgent.create(settings, stores=stores) as agent:
        await agent.run("Сделай демо", thread_id=KEY.thread, system=KEY.system)


async def test_agent_on_files_threads_in_database(
    tmp_path, monkeypatch, requests_seen, database
):
    monkeypatch.chdir(tmp_path)
    settings = scripted_settings()
    stores = Stores(threads=database.threads, agent=FileAgentStore(settings))
    await _run(settings, stores)
    record = await database.threads.get_thread(KEY)
    assert record is not None and record.info.messages_count
    assert requests_seen[0]["messages"][0]["content"]


async def test_agent_in_database_threads_on_files(
    tmp_path, monkeypatch, requests_seen, database
):
    monkeypatch.chdir(tmp_path)
    settings = scripted_settings()
    await copy_defaults(database.agent)
    await database.agent.put_resource("prompt", Prompt.SYSTEM, OWN.encode())
    threads = FileThreadStore(str(tmp_path / "threads"))
    await _run(settings, Stores(threads=threads, agent=database.agent))
    assert OWN in requests_seen[0]["messages"][0]["content"]
    record = await threads.get_thread(KEY)
    assert record is not None and record.info.messages_count
    assert await database.threads.get_thread(KEY) is None
