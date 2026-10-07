"""Публичный API: нативные Client/Worker + AgentPlugin, AsyncAgent(durable=).

Агент — те же настройки, что у локального ``AsyncAgent``; Temporal —
нативными объектами temporalio; ход исполняет воркер с плагином.
"""

import asyncio
import json
from datetime import timedelta

from gc_core.aio import AsyncAgent
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_core_temporalio import (
    AgentClient,
    AgentPlugin,
    AgentSession,
    RunRequest,
    SessionFailed,
    TemporalDurability,
    UserMessage,
    WorkerConfig,
    run_worker,
)
from live_support import call, temporal_cli
from temporalio.api.enums.v1 import EventType
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.workflow import ActivityConfig
from test_workflow_controls import finish, scripted_http  # noqa: F401


def agent_settings(url, tmp_path, **runtime):
    from gc_core.config_modules.root import AppConfig

    settings = AppConfig.model_validate(
        {
            "backend": {
                "api_mode": "chat_completions",
                "completions_url": url,
                "chat_completion_kwargs": {"stream": False},
            },
            "agent": {
                "model": "scripted",
                "max_steps": 10,
                "use_local_subagents": False,
                "strict_turn_finish_validation": False,
            },
            "logging": {"save_logs": False, "console_trace": False},
        }
    )
    return LoadedSettings(
        settings=settings,
        runtime=RuntimeEnv(launch_root=str(tmp_path), **runtime),
    )


async def test_async_agent_runs_through_a_plugin_worker(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """Тот же AsyncAgent, ходы — на воркере: два хода одного треда, ответ
    каждого; запросы модели — на своей очереди (нативный ActivityConfig)."""
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend([[finish("first")], [finish("second")]])
    settings = agent_settings(url, tmp_path)
    store = DurableFileThreadStore(str(tmp_path / "threads"))
    plugin = AgentPlugin(
        {"shop": settings},
        thread_store=store,
        model_activity=ActivityConfig(
            task_queue="shop-model",
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=5)),
        ),
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with (
            Worker(env.client, task_queue="shop", plugins=[plugin]),
            Worker(env.client, task_queue="shop-model", plugins=[plugin]),
        ):
            agent = await AsyncAgent.create(
                settings,
                durable=TemporalDurability(
                    env.client, task_queue="shop", agent="shop"
                ),
            )
            async with agent:
                first = await asyncio.wait_for(
                    agent.run("first task", thread_id="t1"), 60
                )
                second = await asyncio.wait_for(
                    agent.run("second task", thread_id="t1"), 60
                )
            session = AgentSession(
                env.client, agent="shop", thread_id="t1", task_queue="shop"
            )
            queues = set()
            # Сессия делает Continue-As-New после хода: activities — во всех
            # run одного workflow.
            async for run in env.client.list_workflows(
                f'WorkflowId = "{session.workflow_id}"'
            ):
                handle = env.client.get_workflow_handle(
                    session.workflow_id, run_id=run.run_id
                )
                async for event in handle.fetch_history_events():
                    if (
                        event.event_type
                        == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
                    ):
                        attrs = event.activity_task_scheduled_event_attributes
                        queues.add(
                            (attrs.activity_type.name, attrs.task_queue.name)
                        )
            await session.close()
    assert "first" in json.dumps(first, ensure_ascii=False)
    assert "second" in json.dumps(second, ensure_ascii=False)
    assert len(requests) == 2
    contents = [m.get("content") for m in store.load_messages("sdk", "t1")]
    assert "first task" in contents and "second task" in contents
    assert ("agent.model.request", "shop-model") in queues
    assert all(
        queue == "shop"
        for name, queue in queues
        if name != "agent.model.request"
    )


async def test_session_ask_and_text_answer_mode_from_runtime(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """Рантайм агента (text_answer_mode) доходит до воркера вместе с
    настройками: текстовый ответ принимается как ответ хода."""
    monkeypatch.chdir(tmp_path)
    url, _requests, script = scripted_http
    script.append(
        {
            "id": "text",
            "object": "chat.completion",
            "model": "scripted",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Ответ: 1350"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "total_tokens": 6,
            },
        }
    )
    settings = agent_settings(url, tmp_path, text_answer_mode="allow")
    plugin = AgentPlugin(
        {"shop": settings},
        thread_store=DurableFileThreadStore(str(tmp_path / "threads")),
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(env.client, task_queue="shop", plugins=[plugin]):
            session = AgentSession(
                env.client, agent="shop", thread_id="t2", task_queue="shop"
            )
            answer = await asyncio.wait_for(session.ask("сколько?"), 60)
            await session.close()
    assert "1350" in json.dumps(answer, ensure_ascii=False)


async def test_json_worker_config_runs_the_same_plugin(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """``gc-temporal-worker --config`` — тот же плагин: run_worker по
    конфигу исполняет ход клиента AgentClient."""
    monkeypatch.chdir(tmp_path)
    url, _requests, script = scripted_http
    script.append([finish("from json")])
    core = agent_settings(url, tmp_path).settings.model_dump(mode="json")
    config = WorkerConfig.model_validate(
        {
            "workflow_queue": "json-q",
            "model_queue": "json-q",
            "resource_queue": "json-q",
            "state_dir": str(tmp_path),
            "profiles": {"p": {"workspace": str(tmp_path), "core": core}},
        }
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        worker = asyncio.create_task(run_worker(config, client=env.client))
        try:
            client = AgentClient(env.client, workflow_queue="json-q")
            await client.submit_with_start(
                "json-session",
                RunRequest("p", "files", "sdk", "t3", [], keep_alive=True),
                UserMessage("m1", "task"),
            )
            reply = await asyncio.wait_for(
                env.client.get_workflow_handle("json-session").execute_update(
                    "wait_for_message", "m1"
                ),
                60,
            )
            await client.close_session("json-session")
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    assert "from json" in json.dumps(reply["answer"], ensure_ascii=False)


async def test_settings_mismatch_between_app_and_worker_fails_the_turn(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """Приложение и воркер с разными настройками агента: ход не идёт молча по
    профилю воркера — отказ с перечнем различающихся секций."""
    import pytest

    monkeypatch.chdir(tmp_path)
    url, requests, _script = scripted_http
    worker_settings = agent_settings(url, tmp_path)
    app_settings = worker_settings.with_overrides({"agent.max_steps": 11})
    plugin = AgentPlugin(
        {"shop": worker_settings},
        thread_store=DurableFileThreadStore(str(tmp_path / "threads")),
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(env.client, task_queue="shop", plugins=[plugin]):
            agent = await AsyncAgent.create(
                app_settings,
                durable=TemporalDurability(
                    env.client, task_queue="shop", agent="shop"
                ),
            )
            with pytest.raises(
                SessionFailed, match="различаются в секциях: agent"
            ):
                await asyncio.wait_for(agent.run("task", thread_id="t5"), 60)
    assert not requests  # до модели дело не дошло


async def test_durable_send_and_stop_follow_local_semantics(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """send во время хода — хвост этого run (run ждёт его ход); send между
    ходами — без нового хода, уходит со следующим run; stop вне хода —
    ничего (как у локального AsyncAgent)."""
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    loop = asyncio.get_running_loop()
    holder = {}

    def calc_and_send():
        loop.call_soon_threadsafe(
            holder["agent"].send, {"role": "user", "content": "дополнение"}
        )
        return [call("calc", "exec_calculator", {"expression": "2+2"})]

    script.extend([calc_and_send, [finish("first")]])
    settings = agent_settings(url, tmp_path)
    store = DurableFileThreadStore(str(tmp_path / "threads"))
    plugin = AgentPlugin({"shop": settings}, thread_store=store)
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
            holder["agent"] = agent
            agent.stop()  # вне хода — ничего
            first = await asyncio.wait_for(
                agent.run("первый", thread_id="t6"), 60
            )
            session = AgentSession(
                env.client, agent="shop", thread_id="t6", task_queue="shop"
            )
            status = await session.status()
            # run вернул ответ последнего хода — того, что обработал хвост.
            assert first == status["answer"]
            after_first = len(requests)
            agent.send({"role": "user", "content": "между ходами"})
            await asyncio.sleep(1.5)
            assert len(requests) == after_first  # нового хода нет
            script.append([finish("second")])
            second = await asyncio.wait_for(
                agent.run("второй", thread_id="t6"), 60
            )
            await agent.aclose()
            await session.close()
    contents = [m.get("content") for m in store.load_messages("sdk", "t6")]
    assert "дополнение" in contents
    assert contents.index("второй") < contents.index("между ходами")
    assert "second" in json.dumps(second, ensure_ascii=False)
