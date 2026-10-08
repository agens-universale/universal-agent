"""Агент шагом своего workflow: два хода одного треда внутри бизнес-процесса,
вопрос человеку — ответ через ID дочернего workflow шага."""

import asyncio
import json

from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_core_temporalio import AgentPlugin, AgentSession, step_workflow_id
from live_support import call, temporal_cli
from order_workflow_fixture import OrderWorkflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_plugin_live import agent_settings
from test_workflow_controls import finish, scripted_http  # noqa: F401


async def test_agent_step_inside_a_business_workflow(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [
            [call("ask", "turn_ask_human", {"question": "Какой адрес?"})],
            [finish("checked")],
            [finish("delivery ok")],
        ]
    )
    store = DurableFileThreadStore(str(tmp_path / "threads"))
    plugin = AgentPlugin(
        {"shop": agent_settings(url, tmp_path)},
        thread_store=store,
        human_input=True,  # шаг задаёт вопрос, тест отвечает
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(
            env.client,
            task_queue="orders",
            workflows=[OrderWorkflow],
            plugins=[plugin],
        ):
            handle = await env.client.start_workflow(
                OrderWorkflow.run, "42", id="order-42", task_queue="orders"
            )
            # Вопрос агента первого шага — по предсказуемому ID шага.
            step = AgentSession(
                env.client,
                agent="shop",
                thread_id="order-42",
                task_queue="orders",
                workflow_id=step_workflow_id("order-42", "order-42"),
            )
            async with asyncio.timeout(60):
                while True:
                    try:
                        status = await step.status()
                    except Exception:  # noqa: BLE001 — шаг ещё не начался
                        status = {}
                    if status.get("pending_human"):
                        break
                    await asyncio.sleep(0.1)
            await step.answer_human(
                status["pending_human"]["request_id"], "Тверская, 1"
            )
            result = await asyncio.wait_for(handle.result(), 60)
    assert "checked" in json.dumps(result["first"], ensure_ascii=False)
    assert "delivery ok" in json.dumps(result["second"], ensure_ascii=False)
    assert len(requests) == 3
    contents = [
        m.get("content") for m in store.load_messages("sdk", "order-42")
    ]
    assert "Проверь заказ 42" in contents and "А доставка?" in contents
    # Шаги закончились — тред свободен для следующих сессий.
    assert (
        store.read_snapshot("sdk", "order-42", include_state=False).owner_id
        is None
    )


async def test_terminated_parent_releases_the_step_thread(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """Родителя снимают (terminate) посреди шага: ребёнку приходит отмена
    (``REQUEST_CANCEL`` по умолчанию у ``agent_step``), он снимает владение
    тредом — тред свободен без ``gc-temporal unlock``."""
    monkeypatch.chdir(tmp_path)
    url, _requests, script = scripted_http
    script.append(
        [call("ask", "turn_ask_human", {"question": "Какой адрес?"})]
    )
    store = DurableFileThreadStore(str(tmp_path / "threads"))
    plugin = AgentPlugin(
        {"shop": agent_settings(url, tmp_path)},
        thread_store=store,
        human_input=True,  # шаг ждёт ответа — тут его и снимают
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(
            env.client,
            task_queue="orders",
            workflows=[OrderWorkflow],
            plugins=[plugin],
        ):
            handle = await env.client.start_workflow(
                OrderWorkflow.run, "7", id="order-7", task_queue="orders"
            )
            step = AgentSession(
                env.client,
                agent="shop",
                thread_id="order-7",
                task_queue="orders",
                workflow_id=step_workflow_id("order-7", "order-7"),
            )
            async with asyncio.timeout(60):
                while True:
                    try:
                        status = await step.status()
                    except Exception:  # шаг ещё не начался
                        status = {}
                    if status.get("pending_human"):
                        break
                    await asyncio.sleep(0.1)
            await handle.terminate("test")

            def owner():
                return store.read_snapshot(
                    "sdk", "order-7", include_state=False
                ).owner_id

            async with asyncio.timeout(30):
                while owner() is not None:
                    await asyncio.sleep(0.2)
