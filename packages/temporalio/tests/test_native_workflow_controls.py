"""Real native delivery/HITL across turns and Continue-As-New, with HTTP replay."""

import asyncio
import json
import shutil
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gc_ua_temporalio.native_contracts import NativeRunRequest
from gc_ua_temporalio.native_provider import NativeWorkerConfig
from gc_ua_temporalio.native_worker import build_native_runtime, worker_options
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from gc_ua_temporalio.native_contracts import UserMessage
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from live_support import call


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


@pytest.fixture
def scripted_http():
    requests, script = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
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
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
async def runtime(tmp_path, url, *, core_extra=None):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
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
    config = NativeWorkerConfig.model_validate(
        {
            "workflow_queue": "native-controls",
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
    contexts, native, threads = build_native_runtime(config)
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            (options,) = worker_options(config, native, threads)
            async with Worker(
                env.client,
                **options,
            ):
                yield env, contexts, config
    finally:
        await contexts.aclose()


def request(**kwargs):
    return NativeRunRequest(
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
            status = await handle.query(NativeAgentWorkflow.status)
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
            NativeAgentWorkflow.run,
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
            NativeAgentWorkflow.submit_message,
            UserMessage("next", "second task"),
        )
        assert not receipt["duplicate"]
        assert (
            await handle.execute_update(
                NativeAgentWorkflow.submit_message,
                UserMessage("next", "second task"),
            )
        )["duplicate"]
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(
                NativeAgentWorkflow.submit_message,
                UserMessage("next", "changed task"),
            )
        await wait_status(handle, lambda s: s["turns"] == 2)
        await handle.signal(NativeAgentWorkflow.close_session)
        result = await asyncio.wait_for(handle.result(), 30)
        assert result["turns"] == 2 and "second" in str(result["answer"])
        assert not result["undelivered"] and len(requests) == 2
        history = contexts.stores["files"].load_messages("test", "controls")
        assert sum(m.get("content") == "second task" for m in history) == 1
        first = await env.client.get_workflow_handle(
            "controls", run_id=first_run
        ).fetch_history()
        await Replayer(workflows=[NativeAgentWorkflow]).replay_workflow(first)
        await Replayer(workflows=[NativeAgentWorkflow]).replay_workflow(
            await handle.fetch_history()
        )
        assert len(requests) == 2


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
            NativeAgentWorkflow.run,
            request(),
            id="human",
            task_queue="native-controls",
        )
        status = await wait_status(
            handle, lambda s: s["phase"] == "waiting_human"
        )
        request_id = status["pending_human"]["request_id"]
        if stop:
            await handle.signal(NativeAgentWorkflow.stop)
        else:
            assert await handle.execute_update(
                NativeAgentWorkflow.answer_human, args=[request_id, "blue"]
            )
        result = await asyncio.wait_for(handle.result(), 30)
        history = contexts.stores["files"].load_messages("test", "controls")
        assert any("Choose a value" in str(m) for m in history)
        assert len(requests) == (1 if stop else 2)
        if not stop:
            assert "answered" in str(result["answer"])
            assert any("blue" in str(m) for m in history)
        await Replayer(workflows=[NativeAgentWorkflow]).replay_workflow(
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
            NativeAgentWorkflow.run,
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
            NativeAgentWorkflow.answer_human,
            args=[request_id, "blue", "reply-1"],
        )
        await wait_status(
            handle, lambda s: s["turns"] == 1 and s["phase"] == "idle"
        )
        async with asyncio.timeout(30):
            while (await handle.describe()).run_id == first_run:
                await asyncio.sleep(0.05)
        assert await handle.execute_update(
            NativeAgentWorkflow.answer_human,
            args=[request_id, "blue", "reply-1"],
        )
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(
                NativeAgentWorkflow.answer_human,
                args=[request_id, "red", "reply-1"],
            )
        await handle.signal(NativeAgentWorkflow.close_session)
        assert (await asyncio.wait_for(handle.result(), 30))["turns"] == 1
        assert len(requests) == 2
        for run_id in (first_run, None):
            saved = await env.client.get_workflow_handle(
                "human-receipt", run_id=run_id
            ).fetch_history()
            await Replayer(workflows=[NativeAgentWorkflow]).replay_workflow(
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
            NativeAgentWorkflow.run,
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
        meta = store.load_meta("test", "controls")
        usage = meta["token_usage"]["by_category"]["structured_output"]
        assert usage["input"] == 12 and usage["output"] == 2
        assert not any(
            "value" in m.get("output", "")
            for m in store.load_messages("test", "controls")
        )
        assert not contexts._contexts, (
            "Final release must free worker contexts after accounting"
        )
        await Replayer(workflows=[NativeAgentWorkflow]).replay_workflow(
            await handle.fetch_history()
        )
        assert len(requests) == 2 and meta == store.load_meta(
            "test", "controls"
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
            NativeAgentWorkflow.run,
            request(keep_alive=True),
            id="controls",
            task_queue="native-controls",
        )
        status = await wait_status(
            handle, lambda s: s["phase"] == "running" and requests
        )
        await handle.execute_update(
            NativeAgentWorkflow.submit_message, UserMessage("m2", "SECOND")
        )
        await handle.execute_update(
            NativeAgentWorkflow.stop_turn, status["turn_id"]
        )
        status = await wait_status(
            handle, lambda s: s["turns"] == 1 and s["undelivered"]
        )
        assert [m["message_id"] for m in status["undelivered"]] == ["m2"]
        receipt = await handle.execute_update(
            NativeAgentWorkflow.submit_message, UserMessage("m2", "SECOND")
        )
        assert receipt["duplicate"] is False
        status = await wait_status(
            handle, lambda s: s["turns"] == 2 and s["phase"] == "idle"
        )
        assert status["undelivered"] == []
        assert any("SECOND" in json.dumps(body) for body in requests[-1:])
        await handle.signal(NativeAgentWorkflow.close_session)
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
            NativeAgentWorkflow.run,
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
                    if activity.activity_type.name == "agent.model.request"
                ]
                if attempts and attempts[0] >= 2:
                    break
                await asyncio.sleep(0.1)
        await handle.execute_update(
            NativeAgentWorkflow.stop_turn, status["turn_id"]
        )
        result = await asyncio.wait_for(handle.result(), 15)
        assert result["answer"] is None
        assert (await handle.query(NativeAgentWorkflow.status))[
            "phase"
        ] == "stopped"
        history = await handle.fetch_history()
    await Replayer(workflows=[NativeAgentWorkflow]).replay_workflow(history)


def test_message_limit_counts_utf8_bytes_not_chars():
    """Запись истории ограничена байтами: 600 тыс. символов кириллицы — 1,2 МБ,
    больше предела, хотя символов меньше миллиона."""
    from gc_ua_temporalio.native_contracts import MAX_MESSAGE_BYTES

    UserMessage("m", "a" * MAX_MESSAGE_BYTES).validate()
    with pytest.raises(ValueError, match="bytes"):
        UserMessage("m", "я" * 600_000).validate()
    with pytest.raises(ValueError, match="bytes"):
        UserMessage("m", "\ud800" * (MAX_MESSAGE_BYTES // 3 + 1)).validate()


async def test_child_signal_failure_does_not_mask_child_error():
    """Ребёнок упал, а сигнал родителю не дошёл: наружу — ошибка ребёнка."""
    from gc_ua_temporalio.native_child_workflow import drain_signals

    async def broken_signal():
        raise RuntimeError("parent is closed")

    task = asyncio.ensure_future(broken_signal())
    await drain_signals([task], failed=True)  # не поднимает
    task = asyncio.ensure_future(broken_signal())
    with pytest.raises(RuntimeError, match="parent is closed"):
        await drain_signals([task], failed=False)
    await drain_signals([], failed=False)
