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
    assert ("universal_agent.model.request", "shop-model") in queues
    # Контекст хода отпускается на обеих очередях: activity модели собирает
    # на своей очереди свой.
    assert ("universal_agent.context.release", "shop-model") in queues
    assert all(
        queue == "shop"
        for name, queue in queues
        if name
        not in (
            "universal_agent.model.request",
            "universal_agent.context.release",
        )
    )


async def test_separate_model_worker_releases_its_contexts(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """Очередь модели — отдельный процесс со своим плагином (docs/temporal.md,
    «Размещение»): контекст хода, который собрал воркер модели, освобождается
    там же, а не копится до закрытия сессии (ревью PR #135)."""
    monkeypatch.chdir(tmp_path)
    url, _requests, script = scripted_http
    script.extend([[finish("first")], [finish("second")]])
    settings = agent_settings(url, tmp_path)
    model_activity = ActivityConfig(
        task_queue="split-model",
        start_to_close_timeout=timedelta(minutes=2),
        retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=5)),
    )

    def plugin():
        return AgentPlugin(
            {"shop": settings},
            thread_store=DurableFileThreadStore(str(tmp_path / "threads")),
            model_activity=model_activity,
        )

    session_plugin, model_plugin = plugin(), plugin()
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with (
            Worker(env.client, task_queue="split", plugins=[session_plugin]),
            Worker(
                env.client, task_queue="split-model", plugins=[model_plugin]
            ),
        ):
            agent = await AsyncAgent.create(
                settings,
                durable=TemporalDurability(
                    env.client, task_queue="split", agent="shop"
                ),
            )
            async with agent:
                for text in ("first task", "second task"):
                    await asyncio.wait_for(agent.run(text, thread_id="t1"), 60)
                # Ход окончен — воркер модели уже не держит его контекст,
                # хотя сессия ещё открыта.
                assert model_plugin.provider.idle_owners(0) == []
                assert not model_plugin.activities_host._messages
            await AgentSession(
                env.client, agent="shop", thread_id="t1", task_queue="split"
            ).close()


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
                AgentSession(
                    env.client,
                    agent="p",
                    thread_id="t3",
                    task_queue="json-q",
                    workflow_id="json-session",
                ).wait("m1"),
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


async def test_send_with_other_settings_is_rejected_and_session_continues(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """Сессия идёт с настройками приложения A; приложение B с другими
    настройками пишет в тот же тред — его сообщение отклонено сразу
    (``SettingsMismatch``), сессия A работает дальше."""
    import pytest
    from gc_core_temporalio import SettingsMismatch

    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend([[finish("first")], [finish("second")]])
    settings = agent_settings(url, tmp_path)
    plugin = AgentPlugin(
        {"shop": settings},
        thread_store=DurableFileThreadStore(str(tmp_path / "threads")),
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(env.client, task_queue="shop", plugins=[plugin]):
            same = AgentSession(
                env.client,
                agent="shop",
                thread_id="t9",
                task_queue="shop",
                settings=settings,
            )
            other = AgentSession(
                env.client,
                agent="shop",
                thread_id="t9",
                task_queue="shop",
                settings=settings.with_overrides({"agent.max_steps": 11}),
            )
            await asyncio.wait_for(same.ask("первое"), 60)
            with pytest.raises(SettingsMismatch, match="agent"):
                await other.send("чужое")
            second = await asyncio.wait_for(same.ask("второе"), 60)
            await same.close()
    assert "second" in json.dumps(second, ensure_ascii=False)
    assert len(requests) == 2


def test_fingerprint_ignores_process_local_paths_and_ui_sections(tmp_path):
    """Те же настройки из разных каталогов (пути промптов абсолютны от
    каталога конфига) и с разными tui/pricing — один отпечаток; поведение
    агента (max_steps) — другой."""
    from gc_core.config_modules.root import AppConfig, resolve_paths
    from gc_core_temporalio.public_config import settings_fingerprint

    document = {
        "summary": {"chat_completions_summary_prompt_path": "p/summary.md"},
        "intent_scope": {
            "intent_scope_tool_summary_prompt_path": "p/tool.md",
        },
        "security": {"write_guard": {"extra_paths": ["data"]}},
    }
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = resolve_paths(
        AppConfig.model_validate(document), str(tmp_path / "a")
    )
    second = resolve_paths(
        AppConfig.model_validate(
            {
                **document,
                "tui": {"stream_display": "answer"},
                "pricing": {"currency": "EUR"},
            }
        ),
        str(tmp_path / "b"),
    )
    assert (
        first.summary.chat_completions_summary_prompt_path
        != second.summary.chat_completions_summary_prompt_path
    )
    assert settings_fingerprint(first) == settings_fingerprint(second)
    changed = AppConfig.model_validate({**document, "agent": {"max_steps": 3}})
    assert settings_fingerprint(changed) != settings_fingerprint(first)


async def test_without_human_the_turn_never_waits_for_an_answer(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """По умолчанию человека нет: инструмента вопроса у агента нет, и даже
    если модель позовёт его, ход идёт дальше — сессия не встаёт в
    ``waiting_human``."""
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [
            [call("ask", "turn_ask_human", {"question": "Какой адрес?"})],
            [finish("done")],
        ]
    )
    plugin = AgentPlugin(
        {"shop": agent_settings(url, tmp_path)},
        thread_store=DurableFileThreadStore(str(tmp_path / "threads")),
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(env.client, task_queue="shop", plugins=[plugin]):
            session = AgentSession(
                env.client, agent="shop", thread_id="t10", task_queue="shop"
            )
            answer = await asyncio.wait_for(session.ask("сделай"), 60)
            await session.close()
    assert "done" in json.dumps(answer, ensure_ascii=False)
    tools = {
        tool.get("function", {}).get("name")
        for tool in requests[0].get("tools", [])
    }
    assert "turn_ask_human" not in tools


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


async def test_durable_run_batches_messages_and_closes_threadless_sessions(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """Список сообщений run — один ход (как локально); run без thread_id не
    оставляет вечную сессию, держащую тред. ID сессии зависит от агента."""
    from gc_core_temporalio import session_workflow_id
    from temporalio.client import WorkflowExecutionStatus

    assert session_workflow_id(
        "a", "default", "sdk", "t"
    ) != session_workflow_id("b", "default", "sdk", "t")
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend([[finish("batch")], [finish("threadless")]])
    settings = agent_settings(url, tmp_path)
    plugin = AgentPlugin(
        {"shop": settings},
        thread_store=DurableFileThreadStore(str(tmp_path / "threads")),
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
            await asyncio.wait_for(
                agent.run(["раз", "два", "три"], thread_id="t7"), 60
            )
            assert len(requests) == 1  # все три — в одном ходе
            first_input = json.dumps(requests[0], ensure_ascii=False)
            assert all(word in first_input for word in ("раз", "два", "три"))
            await asyncio.wait_for(agent.run("без треда"), 60)
            await agent.aclose()
            # Закрытие асинхронное (сессия доделывает принятое и снимает
            # владение тредом): ждать, пока одноразовая сессия завершится.
            async with asyncio.timeout(30):
                while True:
                    running = [
                        item
                        async for item in env.client.list_workflows(
                            "ExecutionStatus = 'Running'"
                        )
                    ]
                    if len(running) <= 1:
                        break
                    await asyncio.sleep(0.5)
    # Осталась только сессия t7: одноразовая закрылась.
    assert len(running) == 1, [item.id for item in running]
    assert running[0].status == WorkflowExecutionStatus.RUNNING
