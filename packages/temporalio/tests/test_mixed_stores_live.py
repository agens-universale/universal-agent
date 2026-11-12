"""Плагин Temporal со связкой хранилищ: треды и ресурсы агента — в SQLite
(ресурсы залиты из пакета, системный промпт свой). Ход идёт на воркере,
модель видит промпт из базы, тред — в базе."""

import asyncio

from gc_core.aio import AsyncAgent
from gc_core.agent_store import Prompt
from gc_core.stores import Stores
from gc_core.threads.models import ThreadKey
from gc_core_temporalio import AgentPlugin, AgentSession, TemporalDurability
from gc_stores.sqlite import SQLiteAgentStore, SQLiteThreadStore
from gc_stores.sync import copy_defaults
from live_support import temporal_cli
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_plugin_live import agent_settings
from test_workflow_controls import finish, scripted_http  # noqa: F401

OWN = "Системный промпт агента из базы."


async def test_plugin_runs_on_stores_in_sqlite(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.append([finish("из базы")])
    settings = agent_settings(url, tmp_path)
    agent_store = SQLiteAgentStore(tmp_path / "agent.db", agent_id="shop")
    await copy_defaults(agent_store)
    await agent_store.put_resource("prompt", Prompt.SYSTEM, OWN.encode())
    threads = SQLiteThreadStore(tmp_path / "threads.db")
    plugin = AgentPlugin(
        {"shop": settings}, stores=Stores(threads=threads, agent=agent_store)
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(env.client, task_queue="shop", plugins=[plugin]):
            agent = await AsyncAgent.create(
                settings,
                durable=TemporalDurability(
                    env.client, task_queue="shop", agent="shop"
                ),
            )
            async with agent:
                await asyncio.wait_for(agent.run("задача", thread_id="t1"), 60)
            session = AgentSession(
                env.client, agent="shop", thread_id="t1", task_queue="shop"
            )
            await session.close()
    assert OWN in requests[0]["messages"][0]["content"]
    record = await threads.get_thread(ThreadKey(system="sdk", thread="t1"))
    assert record is not None and record.info.messages_count
