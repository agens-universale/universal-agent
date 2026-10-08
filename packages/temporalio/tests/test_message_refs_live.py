"""Вход activity модели — ссылки на уже переданные сообщения.

Без внешнего хранилища payload (всё в истории Temporal): длинный тред и
длинный ход не упираются в лимит записи Temporal (2 МБ), а история хода
растёт на новые сообщения, а не на весь контекст за шаг.
"""

import asyncio
import json
import shutil
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_core_temporalio.message_refs import (
    REF_KEY,
    SentMessages,
    decode,
    message_hash,
)
from gc_core_temporalio.contracts import RunRequest
from gc_core_temporalio.provider import WorkerConfig
from gc_core_temporalio.worker import build_runtime, worker_options
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from live_support import call, temporal_cli
from temporalio.api.enums.v1 import EventType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

LIMIT = 2 * 1024 * 1024


def finish():
    return call(
        "finish",
        "turn_finish",
        {
            "solution_type": "full_solution",
            "task": "test",
            "task_report": "done",
            "handoff": "{}",
        },
    )


@pytest.fixture
def growing_model():
    """Модель читает файл ``steps`` раз (контекст растёт), затем finish.
    Запоминает размер каждого тела запроса — провайдер должен получать
    полный контекст."""
    bodies = []
    plan = {"steps": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["content-length"]))
            body = json.loads(raw)
            if not body.get("tools"):
                reply = {"role": "assistant", "content": "ok"}
                finish_reason = "stop"
            else:
                bodies.append(len(raw))
                step = len(bodies)
                name, arguments = plan.get("by_step", {}).get(
                    step
                ) or plan.get("tool", ("fs_read_file", {"path": "big.txt"}))
                tool = (
                    call(f"r{step}", name, arguments)
                    if step <= plan["steps"]
                    else finish()
                )
                reply = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool],
                }
                finish_reason = "tool_calls"
            data = json.dumps(
                {
                    "id": f"r{len(bodies)}",
                    "object": "chat.completion",
                    "model": "scripted",
                    "choices": [
                        {
                            "index": 0,
                            "message": reply,
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 40,
                        "completion_tokens": 10,
                        "total_tokens": 50,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            bodies,
            plan,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@asynccontextmanager
async def runtime(tmp_path, url, *, persist="step", core_extra=None):
    binary = temporal_cli()
    config = WorkerConfig.model_validate(
        {
            "workflow_queue": "refs",
            "model_queue": "refs",
            "resource_queue": "refs",
            "state_dir": str(tmp_path),
            "profiles": {
                "test": {
                    "workspace": str(tmp_path),
                    "core": {
                        "backend": {
                            "api_mode": "chat_completions",
                            "completions_url": url,
                            "chat_completion_kwargs": {"stream": False},
                        },
                        "agent": {
                            "model": "scripted",
                            "max_steps": 200,
                            "max_tool_calls": 400,
                            "use_local_subagents": False,
                            "strict_turn_finish_validation": False,
                        },
                        "tui": {"text_answer_mode": "deny"},
                        "thread_store": {"persist": persist},
                        "logging": {
                            "save_logs": False,
                            "console_trace": False,
                        },
                        **(core_extra or {}),
                    },
                }
            },
        }
    )
    contexts, native, threads = build_runtime(config)
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            (options,) = worker_options(config, native, threads)
            async with Worker(env.client, **options):
                yield env, native
    finally:
        await contexts.aclose()


def _request(text="go"):
    return RunRequest("test", "files", "test", "refs", [text], "refs", "refs")


async def _payload_sizes(handle):
    """(наибольшая запись истории, входы activity модели по порядку, вся
    история в байтах)."""
    largest, model_inputs, total = 0, [], 0
    names = {}
    async for event in handle.fetch_history_events():
        total += event.ByteSize()
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            size = sum(p.ByteSize() for p in attrs.input.payloads)
            names[event.event_id] = attrs.activity_type.name
            if attrs.activity_type.name == "universal_agent.model.request":
                model_inputs.append(size)
            largest = max(largest, size)
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            attrs = event.activity_task_completed_event_attributes
            largest = max(
                largest, sum(p.ByteSize() for p in attrs.result.payloads)
            )
    return largest, model_inputs, total


async def test_long_thread_and_long_turn_stay_below_temporal_limits(
    tmp_path, monkeypatch, growing_model
):
    """Тред 6 МБ + ход 30 шагов по 40 КБ, всё в истории Temporal: каждая запись
    истории меньше 2 МБ, вход модели на шаг — новые сообщения и ссылки (а не
    весь контекст), провайдер получает весь контекст."""
    monkeypatch.chdir(tmp_path)
    url, bodies, plan = growing_model
    plan["steps"] = 30
    (tmp_path / "big.txt").write_text("y" * 40_000)
    store = DurableFileThreadStore(str(tmp_path / "threads"))
    store.append_messages(
        "test",
        "refs",
        [
            {
                "id": f"old{i}",
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"{i}:" + "x" * 50_000,
            }
            for i in range(120)
        ],
    )
    async with runtime(tmp_path, url) as (env, _native):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            _request(),
            id="refs-long",
            task_queue="refs",
        )
        result = await asyncio.wait_for(handle.result(), 240)
        largest, model_inputs, total = await _payload_sizes(handle)
        history = await handle.fetch_history()
    assert "done" in json.dumps(result, ensure_ascii=False)
    assert len(model_inputs) == 31
    # Провайдер получил весь контекст: тред 6 МБ и выросший ход.
    assert bodies[0] > 6_000_000 and bodies[-1] > bodies[0] + 500_000
    # История: ни одна запись не близка к лимиту. Вход модели на шаг —
    # новый вывод инструмента (40 КБ) и ссылки (без них — весь контекст,
    # 6–7 МБ на шаг). Вся история — тред (читается один раз, страницами) и
    # новое за шаги: вывод инструмента, его запись в тред, вход модели.
    assert largest < LIMIT
    assert max(model_inputs) < 70_000, model_inputs
    assert total < 6_100_000 + 31 * 200_000, total
    await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(history)


async def test_worker_without_context_gets_messages_resent(
    tmp_path, monkeypatch, growing_model
):
    """Воркер «теряет» память перед каждым запросом (как после перезапуска),
    а тред пишется только в конце хода: ссылок не найти ни в памяти, ни в
    треде — workflow повторяет запрос с сообщениями целиком, ход доходит до
    конца."""
    monkeypatch.chdir(tmp_path)
    url, bodies, plan = growing_model
    plan["steps"] = 4
    (tmp_path / "big.txt").write_text("y" * 1_000)
    async with runtime(tmp_path, url, persist="turn") as (env, native):
        original = native._resolve_message_refs
        resent = []

        async def forgetful(call):
            native._messages.clear()
            try:
                return await original(call)
            except Exception as exc:
                resent.append(type(exc).__name__)
                raise

        native._resolve_message_refs = forgetful
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            _request(),
            id="refs-forget",
            task_queue="refs",
        )
        await asyncio.wait_for(handle.result(), 120)
        history = await handle.fetch_history()
    assert len(bodies) == 5
    assert resent  # промахи были и закрылись повтором целиком
    await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(history)


def test_sent_messages_encode_refs_only_after_confirmation():
    sent = SentMessages()
    first = {"id": "a", "role": "user", "content": "hello"}
    second = {"id": "b", "role": "assistant", "content": "hi"}
    encoded, inline = sent.encode([first])
    assert encoded == [first] and inline == [message_hash(first)]
    # Не подтверждено (activity не завершилась) — снова целиком.
    assert sent.encode([first])[0] == [first]
    sent.confirm(inline)
    encoded, inline = sent.encode([first, second])
    assert encoded[0] == {REF_KEY: message_hash(first)}
    assert encoded[1] == second and inline == [message_hash(second)]
    # Батч повторяет хвост состояния — ссылкой на переданное в этом запросе.
    batch, batch_inline = sent.encode([second], also=inline)
    assert batch == [{REF_KEY: message_hash(second)}] and batch_inline == []
    known = {message_hash(first): first, message_hash(second): second}
    assert decode(encoded, known) == [first, second]
    # Копии: правка результата не трогает память воркера.
    decode(encoded, known)[0]["content"] = "changed"
    assert known[message_hash(first)]["content"] == "hello"
    sent.forget()
    assert sent.encode([first])[0] == [first]


def test_hash_ignores_key_order():
    assert message_hash({"a": 1, "b": [1, 2]}) == message_hash(
        {"b": [1, 2], "a": 1}
    )
    assert message_hash({"a": 1}) != message_hash({"a": 2})


async def test_huge_mcp_output_is_capped_below_the_record_limit(
    tmp_path, monkeypatch, growing_model
):
    """Ответ MCP-сервера в 5 МБ: вывод через границу activity обрезается
    воркером до 1 МБ с пометкой, ход доходит до конца, записи истории меньше
    2 МБ (встроенные инструменты ограничивают вывод сами)."""
    import sys
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    url, bodies, plan = growing_model
    plan["steps"] = 1
    plan["tool"] = ("mcp_counter_big", {"size": 5_000_000})
    server = Path(__file__).with_name("mcp_fixture_server.py")
    mcp = {
        "mcp": {
            "servers": {
                "counter": {
                    "transport": "stdio",
                    "persistent_session": True,
                    "command": sys.executable,
                    "args": [str(server), "stdio", "0"],
                }
            }
        }
    }
    async with runtime(tmp_path, url, core_extra=mcp) as (env, _native):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            _request(),
            id="refs-mcp-big",
            task_queue="refs",
        )
        await asyncio.wait_for(handle.result(), 120)
        largest, _, _ = await _payload_sizes(handle)
    assert largest < LIMIT
    messages = DurableFileThreadStore(str(tmp_path / "threads")).load_messages(
        "test", "refs"
    )
    outputs = [
        str(m.get("output") or m.get("content"))
        for m in messages
        if m.get("type") == "function_call_output" or m.get("role") == "tool"
    ]
    assert any("stored" in o for o in outputs), outputs[:1]


def test_tool_output_cap_counts_temporal_history_size():
    """Потолок — по размеру в истории Temporal (ensure_ascii: кириллица —
    6 байт на символ), а не по UTF-8: обрезанный вывод реально помещается."""
    from gc_core.execution.contracts import ToolResult
    from gc_core_temporalio.activities import (
        TOOL_OUTPUT_LIMIT_BYTES,
        cap_tool_output,
    )
    from temporalio.converter import default

    small = ToolResult("ok")
    assert cap_tool_output(small).output == "ok"
    big = cap_tool_output(ToolResult("я" * 1_000_000))
    payload = default().payload_converter.to_payloads([big])[0]
    assert len(payload.data) < TOOL_OUTPUT_LIMIT_BYTES + 4096
    assert big.output.startswith("я" * 100_000)
    assert "вывод обрезан воркером Temporal" in big.output
    assert "из 1000000 символов" in big.output
    big.output.encode()  # без разорванных символов


async def test_long_turn_written_at_the_end_in_parts(
    tmp_path, monkeypatch, growing_model
):
    """persist: turn (умолчание в Temporal) пишет шаги хода в конце. Ход на
    120 шагов — больше 2 МБ новых сообщений — уходит в тред частями: одна
    операция больше лимита записи навсегда валила бы задачу workflow."""
    monkeypatch.chdir(tmp_path)
    url, bodies, plan = growing_model
    plan["steps"] = 120
    (tmp_path / "big.txt").write_text("y" * 40_000)
    async with runtime(tmp_path, url, persist="turn") as (env, _native):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            _request(),
            id="refs-turn-parts",
            task_queue="refs",
        )
        await asyncio.wait_for(handle.result(), 600)
        largest, _, total = await _payload_sizes(handle)
        history = await handle.fetch_history()
    assert largest < LIMIT
    messages = DurableFileThreadStore(str(tmp_path / "threads")).load_messages(
        "test", "refs"
    )
    outputs = [
        m
        for m in messages
        if m.get("type") == "function_call_output" or m.get("role") == "tool"
    ]
    assert len(outputs) >= 120
    assert len({m["id"] for m in messages}) == len(messages)
    await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(history)


@pytest.fixture
def big_page():
    """HTML-страница 3 МБ текста; считает скачивания."""
    paragraph = "<p>" + "страница с большим текстом " * 40 + "</p>\n"
    text = (
        "<html><head><title>big</title></head><body><article>"
        + paragraph * 1_400
        + "</article></body></html>"
    ).encode()
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/page.txt", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_fetch_url_cache_stays_on_the_worker(
    tmp_path, monkeypatch, growing_model, big_page
):
    """Страница 3 МБ читается ``exec_fetch_url`` двумя кусками: второй — из
    кэша воркера (скачивание одно), кэш не идёт ни во вход и результат
    activity, ни в meta треда — записи истории меньше 2 МБ."""
    monkeypatch.chdir(tmp_path)
    url, _bodies, plan = growing_model
    page, hits = big_page
    plan["steps"] = 2
    plan["by_step"] = {
        1: ("exec_fetch_url", {"url": page, "limit": 1000}),
        2: ("exec_fetch_url", {"url": page, "offset": 1000, "limit": 1000}),
    }
    extra = {"security": {"url_policy": {"enabled": False}}}
    async with runtime(tmp_path, url, core_extra=extra) as (env, native):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            _request(),
            id="refs-fetch-cache",
            task_queue="refs",
        )
        await asyncio.wait_for(handle.result(), 120)
        largest, _, _ = await _payload_sizes(handle)
        history = await handle.fetch_history()
        cached = native.fetch_pages.get(handle.id)
    assert len(hits) == 1, hits
    assert [key for key, _ in cached] == [page]
    assert largest < 200_000, largest
    meta = DurableFileThreadStore(str(tmp_path / "threads")).load_meta(
        "test", "refs"
    )
    assert not (meta.get("shared") or {}).get("fetch_url_cache")
    await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(history)


def test_fetch_page_cache_evicts_whole_sessions_by_size():
    from gc_core_temporalio.activities import FetchPageCache

    cache = FetchPageCache(limit_bytes=1_000)
    cache.put("a", [["u1", {"content": "x" * 400}]])
    cache.put("b", [["u2", {"content": "x" * 400}]])
    cache.get("a")  # «a» свежее «b»
    cache.put("c", [["u3", {"content": "x" * 400}]])
    assert cache.get("b") == [] and cache.get("a") and cache.get("c")
    cache.put("big", [["u4", {"content": "x" * 2_000}]])
    assert cache.get("big") == []  # больше всего лимита — не держим


async def test_cache_miss_on_a_big_cyrillic_thread_does_not_inline_it(
    tmp_path, monkeypatch, growing_model
):
    """Воспроизведение ревью: тред ~3 МБ кириллицы в истории, воркер теряет
    кэш ссылок перед каждым запросом, persist: turn. Повтор не шлёт тред
    одной записью (раньше — PayloadsTooLarge и вечный сбой задачи): шаги
    хода сначала пишутся в тред, сообщения идут ссылками; ход доходит до
    конца, каждая запись истории меньше 2 МБ."""
    monkeypatch.chdir(tmp_path)
    url, bodies, plan = growing_model
    plan["steps"] = 3
    (tmp_path / "big.txt").write_text("щ" * 2_000)
    store = DurableFileThreadStore(str(tmp_path / "threads"))
    store.append_messages(
        "test",
        "refs",
        [
            {
                "id": f"old{i}",
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"{i}:" + "ж" * 10_000,
            }
            for i in range(50)
        ],
    )
    async with runtime(tmp_path, url, persist="turn") as (env, native):
        original = native._resolve_message_refs
        misses = []

        async def forgetful(call):
            native._messages.clear()
            try:
                return await original(call)
            except Exception as exc:
                misses.append(type(exc).__name__)
                raise

        native._resolve_message_refs = forgetful
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            _request(),
            id="refs-big-miss",
            task_queue="refs",
        )
        result = await asyncio.wait_for(handle.result(), 180)
        largest, model_inputs, _total = await _payload_sizes(handle)
    assert "done" in json.dumps(result, ensure_ascii=False)
    assert misses  # промахи были
    assert largest < LIMIT, largest
    assert max(model_inputs) < 600_000, model_inputs
    assert bodies[0] > 1_000_000  # провайдер получил весь контекст
