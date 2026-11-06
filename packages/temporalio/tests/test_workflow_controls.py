"""Real native delivery/HITL across turns and Continue-As-New, with HTTP replay."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gc_core_temporalio.contracts import RunRequest, UserMessage
from gc_core_temporalio.provider import WorkerConfig
from gc_core_temporalio.worker import build_runtime
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from live_support import (
    call,
    temporal_cli,
    thread_messages,
    thread_meta,
    worker_kwargs,
)
from gc_testkit.helpers.crossplatform import python_command
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker


def finish(report="done"):
    return call(
        "finish",
        "turn_finish",
        {
            "solution_type": "full_solution",
            "task": "test",
            "task_report": report,
            "handoff": "{}",
        },
    )


class _Recorded(list):
    """Тела запросов; ``headers`` — их заголовки (имена в нижнем регистре)."""

    def __init__(self):
        super().__init__()
        self.headers = []


@pytest.fixture
def scripted_http():
    requests, script = _Recorded(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.headers.append(
                {name.lower(): value for name, value in self.headers.items()}
            )
            requests.append(
                json.loads(
                    self.rfile.read(int(self.headers["content-length"]))
                )
            )
            item = script.pop(0) if script else [finish()]
            if callable(item):
                item = item()
            response = (
                item
                if isinstance(item, dict)
                else {
                    "id": f"response-{len(requests)}",
                    "object": "chat.completion",
                    "model": "scripted",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": item,
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 40,
                        "completion_tokens": 10,
                        "total_tokens": 50,
                    },
                }
            )
            body = json.dumps(response).encode()
            content_type = "application/json"
            if requests[-1].get("stream") and "choices" in response:
                content_type = "text/event-stream"
                for choice in response["choices"]:
                    delta = choice.pop("message")
                    for index, tool in enumerate(
                        delta.get("tool_calls") or []
                    ):
                        tool["index"] = index
                    choice["delta"] = delta
                response["object"] = "chat.completion.chunk"
                body = (
                    "data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n"
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            requests,
            script,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@asynccontextmanager
async def runtime(
    tmp_path, url, *, core_extra=None, workflows=None, legacy_patches=False
):
    binary = temporal_cli()
    core = {
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
        "tui": {"text_answer_mode": "deny"},
        "logging": {"save_logs": False, "console_trace": False},
        **(core_extra or {}),
    }
    config = WorkerConfig.model_validate(
        {
            "workflow_queue": "native-controls",
            # Сценарии с вопросом человеку и подтверждениями.
            "human_input": True,
            "model_queue": "native-controls",
            "resource_queue": "native-controls",
            "state_dir": str(tmp_path),
            "profiles": {
                "test": {
                    "workspace": str(tmp_path),
                    "core": core,
                }
            },
        }
    )
    contexts, native, threads = build_runtime(config)
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            options = worker_kwargs(config, native, threads)
            if workflows is not None:
                # Свой workflow сессии под тем же именем (фикстура теста).
                options["workflows"] = workflows
            if legacy_patches:
                options["patch_activation_callback"] = lambda _patch: False
            async with Worker(
                env.client,
                **options,
            ):
                yield env, contexts, config
    finally:
        await contexts.aclose()


def request(**kwargs):
    return RunRequest(
        "test",
        "files",
        "test",
        "controls",
        ["first task"],
        "native-controls",
        "native-controls",
        **kwargs,
    )


async def wait_status(handle, predicate):
    async with asyncio.timeout(30):
        while True:
            status = await handle.query(UniversalAgentWorkflow.status)
            if predicate(status):
                return status
            await asyncio.sleep(0.05)


async def test_two_turns_continue_as_new_and_delivery_dedup(
    tmp_path, monkeypatch, scripted_http
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend([[finish("first")], [finish("second")]])
    async with runtime(tmp_path, url) as (env, contexts, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(keep_alive=True),
            id="controls",
            task_queue="native-controls",
        )
        first_run = handle.first_execution_run_id
        await wait_status(
            handle, lambda s: s["turns"] == 1 and s["phase"] == "idle"
        )
        async with asyncio.timeout(30):
            while (await handle.describe()).run_id == first_run:
                await asyncio.sleep(0.05)
        receipt = await handle.execute_update(
            UniversalAgentWorkflow.submit_message,
            UserMessage("next", "second task"),
        )
        assert not receipt["duplicate"]
        assert (
            await handle.execute_update(
                UniversalAgentWorkflow.submit_message,
                UserMessage("next", "second task"),
            )
        )["duplicate"]
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(
                UniversalAgentWorkflow.submit_message,
                UserMessage("next", "changed task"),
            )
        await wait_status(handle, lambda s: s["turns"] == 2)
        await handle.signal(UniversalAgentWorkflow.close_session)
        result = await asyncio.wait_for(handle.result(), 30)
        assert result["turns"] == 2 and "second" in str(result["answer"])
        assert not result["undelivered"] and len(requests) == 2
        history = await thread_messages(
            contexts.stores["files"], "test", "controls"
        )
        assert sum(m.get("content") == "second task" for m in history) == 1
        first = await env.client.get_workflow_handle(
            "controls", run_id=first_run
        ).fetch_history()
        await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(
            first
        )
        await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(
            await handle.fetch_history()
        )
        assert len(requests) == 2


def session_of(env, workflow_id):
    """Ожидание сообщений сессии по её workflow ID (AgentSession.wait)."""
    from gc_core_temporalio import AgentSession

    return AgentSession(
        env.client,
        agent="test",
        thread_id="controls",
        task_queue="native-controls",
        workflow_id=workflow_id,
    )


async def test_wait_returns_the_answer_of_its_own_turn(
    tmp_path, monkeypatch, scripted_http
):
    """«Отправить и дождаться»: ответ — хода, обработавшего сообщение (не
    последнего), и после Continue-As-New; неизвестный ID — отказ; много
    ожидающих клиентов не занимают лимит update сервера (ожидание — query)."""
    monkeypatch.chdir(tmp_path)
    url, _requests, script = scripted_http
    gate = threading.Event()

    def second_turn():
        gate.wait(30)
        return [finish("second")]

    script.extend([[finish("first")], second_turn])
    async with runtime(tmp_path, url) as (env, _contexts, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(keep_alive=True, message_ids=["m1"]),
            id="controls-wait",
            task_queue="native-controls",
        )
        session = session_of(env, "controls-wait")
        first = await asyncio.wait_for(session.wait("m1"), 60)
        assert first["delivered"] and first["turn"] == 1
        assert "first" in str(first["answer"])
        await handle.execute_update(
            UniversalAgentWorkflow.submit_message,
            UserMessage("m2", "second task"),
        )
        # Ход 2 стоит у модели; 12 ожидающих клиентов не мешают update
        # (лимит сервера — 10 одновременных update на workflow).
        waiters = [asyncio.create_task(session.wait("m2")) for _ in range(12)]
        await asyncio.sleep(1)
        await asyncio.wait_for(
            handle.execute_update(
                UniversalAgentWorkflow.submit_message,
                UserMessage("m3", "third"),
            ),
            20,
        )
        gate.set()
        second = await asyncio.wait_for(waiters[0], 60)
        await asyncio.gather(*waiters)
        # m3 пришло во время хода 2 — этот же ход его и обработал; ответ
        # хода 2 — его итог после m3.
        third = await session.wait("m3")
        assert second["turn"] == third["turn"] == 2
        assert second["answer"] == third["answer"]
        # Старое сообщение — ответ своего хода, а не последнего.
        again = await session.wait("m1")
        assert again["turn"] == 1 and "first" in str(again["answer"])
        with pytest.raises(ValueError, match="Unknown message"):
            await session.wait("never-sent")
        await handle.signal(UniversalAgentWorkflow.close_session)
        await asyncio.wait_for(handle.result(), 30)


@pytest.mark.parametrize("stop", [False, True])
async def test_native_human_reply_or_stop_preserves_lifecycle(
    tmp_path, monkeypatch, scripted_http, stop
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [
            [call("ask", "turn_ask_human", {"question": "Choose a value"})],
            [finish("answered")],
        ]
    )
    async with runtime(tmp_path, url) as (env, contexts, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(),
            id="human",
            task_queue="native-controls",
        )
        status = await wait_status(
            handle, lambda s: s["phase"] == "waiting_human"
        )
        request_id = status["pending_human"]["request_id"]
        if stop:
            await handle.signal(UniversalAgentWorkflow.stop)
        else:
            assert await handle.execute_update(
                UniversalAgentWorkflow.answer_human, args=[request_id, "blue"]
            )
        result = await asyncio.wait_for(handle.result(), 30)
        history = await thread_messages(
            contexts.stores["files"], "test", "controls"
        )
        assert any("Choose a value" in str(m) for m in history)
        assert len(requests) == (1 if stop else 2)
        if not stop:
            assert "answered" in str(result["answer"])
            assert any("blue" in str(m) for m in history)
        await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(
            await handle.fetch_history()
        )
        assert len(requests) == (1 if stop else 2)


async def test_human_reply_receipt_survives_continue_as_new(
    tmp_path, monkeypatch, scripted_http
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [
            [call("ask", "turn_ask_human", {"question": "Choose a value"})],
            [finish("answered")],
        ]
    )
    async with runtime(tmp_path, url) as (env, _, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(keep_alive=True),
            id="human-receipt",
            task_queue="native-controls",
        )
        first_run = handle.first_execution_run_id
        status = await wait_status(
            handle, lambda s: s["phase"] == "waiting_human"
        )
        request_id = status["pending_human"]["request_id"]
        assert await handle.execute_update(
            UniversalAgentWorkflow.answer_human,
            args=[request_id, "blue", "reply-1"],
        )
        await wait_status(
            handle, lambda s: s["turns"] == 1 and s["phase"] == "idle"
        )
        async with asyncio.timeout(30):
            while (await handle.describe()).run_id == first_run:
                await asyncio.sleep(0.05)
        assert await handle.execute_update(
            UniversalAgentWorkflow.answer_human,
            args=[request_id, "blue", "reply-1"],
        )
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(
                UniversalAgentWorkflow.answer_human,
                args=[request_id, "red", "reply-1"],
            )
        await handle.signal(UniversalAgentWorkflow.close_session)
        assert (await asyncio.wait_for(handle.result(), 30))["turns"] == 1
        assert len(requests) == 2
        for run_id in (first_run, None):
            saved = await env.client.get_workflow_handle(
                "human-receipt", run_id=run_id
            ).fetch_history()
            await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(
                saved
            )
        assert len(requests) == 2


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter"])
async def test_post_run_result_and_usage_are_accounted_once(
    tmp_path, monkeypatch, scripted_http, reason
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [
            [finish("source outcome")],
            {
                "id": "formatted",
                "object": "chat.completion",
                "model": "scripted",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"value": 42}',
                        },
                        "finish_reason": reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 2,
                    "total_tokens": 14,
                },
            },
        ]
    )
    async with runtime(tmp_path, url) as (env, contexts, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(format_text='JSON with field "value"'),
            id="post-run",
            task_queue="native-controls",
        )
        result = await asyncio.wait_for(handle.result(), 30)
        assert len(requests) == 2
        if reason == "stop":
            assert result["formatted_answer"] == '{"value": 42}'
            assert result["exit_code"] == 0 and result["post_error"] is None
        else:
            assert result["formatted_answer"] is None
            assert result["exit_code"] == 1 and reason in result["post_error"]
        store = contexts.stores["files"]
        meta = await thread_meta(store, "test", "controls")
        usage = meta["token_usage"]["by_category"]["structured_output"]
        assert usage["input"] == 12 and usage["output"] == 2
        assert not any(
            "value" in m.get("output", "")
            for m in await thread_messages(store, "test", "controls")
        )
        assert not contexts._contexts, (
            "Final release must free worker contexts after accounting"
        )
        await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(
            await handle.fetch_history()
        )
        assert len(requests) == 2 and meta == await thread_meta(
            store, "test", "controls"
        )


async def test_resubmit_after_stopped_turn_is_accepted_again(
    tmp_path, monkeypatch, scripted_http
):
    """Сообщение, отброшенное остановкой хода, при повторе с тем же ID
    принимается заново, а не отвечает duplicate навсегда."""
    import time

    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    (tmp_path / "a.txt").write_text("a")

    def slow():
        time.sleep(2.0)
        return [call("r", "fs_read_file", {"path": "a.txt"})]

    script.extend([slow, [finish("first")], [finish("second")]])
    async with runtime(tmp_path, url) as (env, _, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(keep_alive=True),
            id="controls",
            task_queue="native-controls",
        )
        status = await wait_status(
            handle, lambda s: s["phase"] == "running" and requests
        )
        await handle.execute_update(
            UniversalAgentWorkflow.submit_message, UserMessage("m2", "SECOND")
        )
        await handle.execute_update(
            UniversalAgentWorkflow.stop_turn, status["turn_id"]
        )
        status = await wait_status(
            handle, lambda s: s["turns"] == 1 and s["undelivered"]
        )
        assert [m["message_id"] for m in status["undelivered"]] == ["m2"]
        receipt = await handle.execute_update(
            UniversalAgentWorkflow.submit_message, UserMessage("m2", "SECOND")
        )
        assert receipt["duplicate"] is False
        status = await wait_status(
            handle, lambda s: s["turns"] == 2 and s["phase"] == "idle"
        )
        assert status["undelivered"] == []
        assert any("SECOND" in json.dumps(body) for body in requests[-1:])
        await handle.signal(UniversalAgentWorkflow.close_session)
        await handle.result()


async def test_stop_cancels_model_request_retried_without_limit(
    tmp_path, monkeypatch
):
    """Провайдер недоступен, Temporal повторяет запрос без ограничения:
    stop отменяет ожидающую activity модели, ход завершается остановленным,
    а не ждёт конца повторов."""
    monkeypatch.chdir(tmp_path)
    async with runtime(tmp_path, "http://127.0.0.1:1") as (env, _, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(),
            id="controls",
            task_queue="native-controls",
        )
        status = await wait_status(handle, lambda s: s["phase"] == "running")
        async with asyncio.timeout(30):
            while True:
                described = await handle.describe()
                attempts = [
                    activity.attempt
                    for activity in described.raw_description.pending_activities
                    if activity.activity_type.name
                    == "universal_agent.model.request"
                ]
                if attempts and attempts[0] >= 2:
                    break
                await asyncio.sleep(0.1)
        await handle.execute_update(
            UniversalAgentWorkflow.stop_turn, status["turn_id"]
        )
        result = await asyncio.wait_for(handle.result(), 15)
        assert result["answer"] is None
        assert (await handle.query(UniversalAgentWorkflow.status))[
            "phase"
        ] == "stopped"
        history = await handle.fetch_history()
    await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(history)


async def test_stop_while_a_tool_runs_does_not_break_the_workflow(
    tmp_path, monkeypatch, scripted_http
):
    """Слияние develop: снятие ``UserStop`` ядра к ходу workflow не
    привязано. Привязанное, оно на стопе во время тула звало
    ``call_soon_threadsafe``, которого у цикла workflow нет: задача
    workflow падала на каждом повторе. Стоп Temporal — свой (модель,
    субагенты): тул дорабатывает, ход останавливается на границе шага."""
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    command = python_command(tmp_path, "import time\ntime.sleep(3)\n")
    script.append(
        [call("slow", "exec_command", {"command": command, "cwd": "."})]
    )
    no_judge = {"security": {"command_moderation": {"enabled": False}}}
    async with runtime(tmp_path, url, core_extra=no_judge) as (env, _, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(),
            id="controls",
            task_queue="native-controls",
        )
        status = await wait_status(handle, lambda s: s["phase"] == "running")
        async with asyncio.timeout(30):
            while True:
                described = await handle.describe()
                if any(
                    activity.activity_type.name
                    == "universal_agent.tool.execute"
                    for activity in described.raw_description.pending_activities
                ):
                    break
                await asyncio.sleep(0.05)
        await asyncio.wait_for(
            handle.execute_update(
                UniversalAgentWorkflow.stop_turn, status["turn_id"]
            ),
            30,
        )
        await asyncio.wait_for(handle.result(), 30)
        assert (await handle.query(UniversalAgentWorkflow.status))[
            "phase"
        ] == "stopped"
        history = await handle.fetch_history()
    failed = [
        event
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED
    ]
    assert failed == []
    assert len(requests) == 1  # после стопа модель не спрашивали
    await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(history)


def test_message_limit_counts_temporal_history_bytes():
    """Предел — размер в истории Temporal: кириллица там 6 байт на символ
    (ensure_ascii), 200 тысяч символов — 1,2 МБ, больше предела."""
    from gc_core_temporalio.contracts import MAX_MESSAGE_BYTES
    from temporalio.converter import default

    UserMessage("m", "a" * (MAX_MESSAGE_BYTES - 2)).validate()
    with pytest.raises(ValueError, match="bytes"):
        UserMessage("m", "я" * 200_000).validate()
    fits = UserMessage("m", "я" * 160_000)
    fits.validate()
    payload = default().payload_converter.to_payloads([fits])[0]
    assert len(payload.data) < MAX_MESSAGE_BYTES + 1024


async def test_child_signal_failure_does_not_mask_child_error():
    """Ребёнок упал, а сигнал родителю не дошёл: наружу — ошибка ребёнка."""
    from gc_core_temporalio.child_workflow import drain_signals

    async def broken_signal():
        raise RuntimeError("parent is closed")

    task = asyncio.ensure_future(broken_signal())
    await drain_signals([task], failed=True)  # не поднимает
    task = asyncio.ensure_future(broken_signal())
    with pytest.raises(RuntimeError, match="parent is closed"):
        await drain_signals([task], failed=False)
    await drain_signals([], failed=False)


async def test_two_big_cyrillic_messages_keep_records_below_the_limit(
    tmp_path, monkeypatch, scripted_http
):
    """Воспроизведение ревью: два сообщения у предела (кириллица) в одной
    сессии. Журнал намерений в meta копил оба текста, и запись со второго
    хода весила > 2 МБ — дальше вечный сбой. Теперь meta идёт разницей, а
    тексты — ссылками на сообщения: оба хода проходят, записи истории меньше
    лимита, журнал целый."""
    from test_message_refs_live import LIMIT, _payload_sizes

    monkeypatch.chdir(tmp_path)
    url, _requests, script = scripted_http
    script.extend([[finish("first")], [finish("second")]])
    big = "ы" * 160_000  # ~960 КБ в истории Temporal
    async with runtime(tmp_path, url) as (env, contexts, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            RunRequest(
                "test",
                "files",
                "test",
                "big",
                [big],
                "native-controls",
                "native-controls",
                message_ids=["m1"],
                keep_alive=True,
            ),
            id="controls-big",
            task_queue="native-controls",
        )
        session = session_of(env, "controls-big")
        await asyncio.wait_for(session.wait("m1"), 120)
        await handle.execute_update(
            UniversalAgentWorkflow.submit_message,
            UserMessage("m2", "Ж" + big[1:]),
        )
        second = await asyncio.wait_for(session.wait("m2"), 120)
        assert second["turn"] == 2
        largest = 0
        async for run in env.client.list_workflows(
            'WorkflowId = "controls-big"'
        ):
            size, _, _ = await _payload_sizes(
                env.client.get_workflow_handle(
                    "controls-big", run_id=run.run_id
                )
            )
            largest = max(largest, size)
        await handle.signal(UniversalAgentWorkflow.close_session)
        await asyncio.wait_for(handle.result(), 60)
        meta = await thread_meta(contexts.stores["files"], "test", "big")
    assert largest < LIMIT, largest
    # Журнал не урезан (как в локальном режиме): оба запроса целиком —
    # в историю они шли ссылками на тексты сообщений.
    asks = [
        r["content"]
        for r in meta["user_intent_log"]
        if r["action"] == "user_ask"
    ]
    assert asks == [big, "Ж" + big[1:]]


async def test_model_reply_over_the_record_limit_fails_the_turn_not_the_session(
    tmp_path, monkeypatch, scripted_http
):
    """Ответ модели больше записи истории (кириллица): ход отказывает с
    размером в причине, сессия жива — следующее сообщение обрабатывается."""
    monkeypatch.chdir(tmp_path)
    url, _requests, script = scripted_http
    script.extend([[finish("ы" * 400_000)], [finish("second")]])
    async with runtime(tmp_path, url) as (env, _contexts, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            RunRequest(
                "test",
                "files",
                "test",
                "huge",
                ["первое"],
                "native-controls",
                "native-controls",
                message_ids=["m1"],
                keep_alive=True,
            ),
            id="controls-huge",
            task_queue="native-controls",
        )
        session = session_of(env, "controls-huge")
        first = await asyncio.wait_for(session.wait("m1"), 120)
        assert "больше лимита записи" in first["turn_error"]
        assert "байт" in first["turn_error"]
        await handle.execute_update(
            UniversalAgentWorkflow.submit_message, UserMessage("m2", "второе")
        )
        second = await asyncio.wait_for(session.wait("m2"), 120)
        assert second["turn"] == 2
        assert second["turn_error"] is None
        await handle.signal(UniversalAgentWorkflow.close_session)
        result = await asyncio.wait_for(handle.result(), 60)
    assert result["exit_code"] == 0


async def test_submit_batch_is_all_or_nothing(
    tmp_path, monkeypatch, scripted_http
):
    """Отказ на одном сообщении пакета (повтор ID с другим текстом) не
    оставляет принятыми остальные: проверка — до приёма первого."""
    from temporalio.client import WorkflowUpdateFailedError

    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend([[finish("first")], [finish("second")]])
    async with runtime(tmp_path, url) as (env, _, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(keep_alive=True),
            id="controls-batch",
            task_queue="native-controls",
        )
        await wait_status(
            handle, lambda s: s["turns"] == 1 and s["phase"] == "idle"
        )
        await handle.execute_update(
            UniversalAgentWorkflow.submit_message, UserMessage("m1", "ONE")
        )
        await wait_status(
            handle, lambda s: s["turns"] == 2 and s["phase"] == "idle"
        )
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(
                UniversalAgentWorkflow.submit_batch,
                [UserMessage("m2", "NEW"), UserMessage("m1", "CHANGED")],
            )
        state = await handle.query(UniversalAgentWorkflow.message_state, "m2")
        assert state["state"] == "unknown"
        await handle.signal(UniversalAgentWorkflow.close_session)
        await handle.result()


@pytest.mark.parametrize(
    "legacy", [False, True], ids=["recovery", "old_history"]
)
async def test_malformed_tool_json_recovery_and_old_history_replay(
    tmp_path, monkeypatch, scripted_http, legacy
):
    """Новое восстановление меняет модель; старая история сохраняет floor replay."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "example.txt").write_text("test")
    url, requests, script = scripted_http
    bad = call("bad", "fs_read_file", {"path": "example.txt"})
    bad["function"]["arguments"] = '{"path": '
    script.extend(
        [
            [call("read", "fs_read_file", {"path": "example.txt"})],
            [bad],
            [finish()],
            [finish()],
        ]
    )
    extra = {
        "level": "auto",
        "models": {
            "fast": {"model": "fast-model", "url": url, "token": ""},
            "mid": {"model": "mid-model", "url": url, "token": ""},
        },
        "slots": {"light": "fast", "default": "mid"},
    }
    async with runtime(
        tmp_path, url, core_extra=extra, legacy_patches=legacy
    ) as (env, _, _config):
        async with asyncio.timeout(45):
            handle = await env.client.start_workflow(
                UniversalAgentWorkflow.run,
                request(),
                id="malformed-tool-replay",
                task_queue="native-controls",
            )
            result = await handle.result()
            history = await handle.fetch_history()
    assert "done" in str(result["answer"])
    assert [r["model"] for r in requests] == (
        ["mid-model", "mid-model", "fast-model", "mid-model"]
        if legacy
        else ["mid-model", "mid-model", "mid-model"]
    )
    replayer = Replayer(workflows=[UniversalAgentWorkflow])
    await replayer.replay_workflow(history)
