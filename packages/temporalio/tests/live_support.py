"""Shared scripted HTTP provider and helpers for native live tests."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@pytest.fixture
def provider(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            requests.append(request)
            outputs = [
                m for m in request["messages"] if m.get("role") == "tool"
            ]
            if not outputs:
                calls = [
                    call(
                        "memory",
                        "session_memory",
                        {
                            "action": "append",
                            "register": "knowledge",
                            "content": "durable fact",
                        },
                    ),
                    call(
                        "todo",
                        "session_todo",
                        {
                            "todos": [
                                {
                                    "content": "verify recovery",
                                    "status": "completed",
                                }
                            ]
                        },
                    ),
                    call(
                        "read",
                        "fs_read_file",
                        {
                            "path": str(tmp_path / "example.txt")
                            if any(
                                str(tmp_path) in str(m.get("content", ""))
                                for m in request["messages"]
                                if m.get("role") == "user"
                            )
                            else "example.txt"
                        },
                    ),
                ]
            else:
                calls = [
                    call(
                        "finish",
                        "turn_finish",
                        {
                            "solution_type": "full_solution",
                            "task": "demo",
                            "task_report": "Recovered successfully",
                            "handoff": "{}",
                        },
                    )
                ]
            response = {
                "id": f"response-{2 if outputs else 1}",
                "object": "chat.completion",
                "model": "scripted",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": calls,
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
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield (
        f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
        requests,
    )
    server.shutdown()
    server.server_close()
    thread.join()


async def wait_marker(path, process, handle):
    async with asyncio.timeout(60):
        while not path.exists():
            if process.poll() is not None:
                raise AssertionError("Worker exited; see worker log")
            if (await handle.describe()).status.name != "RUNNING":
                await handle.result()
                raise AssertionError("Workflow ended before checkpoint")
            await asyncio.sleep(0.2)


def temporal_cli():
    """Путь к ``temporal`` CLI для живых тестов (dev-сервер Temporal).

    Без CLI тест пропускается — локальной разработке Temporal не обязателен.
    С ``REQUIRE_TEMPORAL=1`` (прогон, который должен проверить Temporal)
    отсутствие CLI — ошибка: иначе живые тесты молча не исполнятся.
    """
    import os
    import shutil

    binary = shutil.which("temporal")
    if binary is None:
        if os.environ.get("REQUIRE_TEMPORAL") == "1":
            pytest.fail(
                "REQUIRE_TEMPORAL=1, but the temporal CLI is not on PATH"
            )
        pytest.skip("Temporal CLI is required")
    return binary


def worker_queues(config):
    """Различные очереди конфига воркера — на каждую свой ``Worker``, как у
    ``gc-temporal-worker``."""
    return list(
        dict.fromkeys(
            [config.workflow_queue, config.model_queue, config.resource_queue]
        )
    )


def worker_kwargs(config, native, threads, *, queue=None):
    """Параметры ``Worker`` живых тестов, собранного без ``AgentPlugin``:
    workflows и activities агента (тот же список, что регистрирует плагин) на
    очереди ``queue`` (по умолчанию — очереди сессий) плюс Worker Versioning
    из конфига."""
    from gc_core_temporalio.child_workflow import UniversalSubagentWorkflow
    from gc_core_temporalio.worker import (
        agent_activities,
        deployment_config,
        workflow_runner,
    )
    from gc_core_temporalio.workflow import UniversalAgentWorkflow

    options = {
        "task_queue": queue or config.workflow_queue,
        "workflows": [UniversalAgentWorkflow, UniversalSubagentWorkflow],
        "activities": agent_activities(native, threads),
        "workflow_runner": workflow_runner(),
    }
    deployment = deployment_config(config)
    if deployment is not None:
        options["deployment_config"] = deployment
    return options


async def thread_messages(store, system, thread):
    """История треда глазами сессии: у записей без ``id`` он синтезирован —
    как при чтении старым файловым хранилищем."""
    from gc_core.threads.history import HISTORY, messages_and_keys
    from gc_core.threads.models import ThreadKey

    key = ThreadKey(system=system, thread=thread)
    entries = await store.read_stream(key, HISTORY)
    return messages_and_keys(store, key, entries)[0]


async def thread_meta(store, system, thread):
    """Meta треда в прежней форме (``meta_from_record``) или ``{}``."""
    from gc_core.threads.meta_map import meta_from_record
    from gc_core.threads.models import ThreadKey

    record = await store.get_thread(ThreadKey(system=system, thread=thread))
    return {} if record is None else meta_from_record(record)


async def thread_owner(store, system, thread):
    """Владелец аренды треда (workflow сессии) или ``None``."""
    from gc_core.threads.models import ThreadKey

    record = await store.get_thread(ThreadKey(system=system, thread=thread))
    lease = record.lease if record is not None else None
    return None if lease is None else lease.owner


async def seed_messages(store, system, thread, messages):
    """Засеять историю треда (как будто её вёл локальный режим) и отпустить
    аренду: сессия Temporal возьмёт тред без захвата."""
    from gc_core.threads.facade import Threads
    from gc_core.threads.models import ThreadKey

    key = ThreadKey(system=system, thread=thread)
    await Threads(store, owner="seed").append_messages(key, list(messages))


async def seed_meta(store, system, thread, meta):
    """Засеять meta треда (режим, план — до первого хода) и отпустить
    аренду: сессия Temporal возьмёт тред без захвата."""
    from gc_core.threads.meta_map import parts_from_meta
    from gc_core.threads.models import ThreadKey
    from gc_core.threads.session_writer import SessionWriter

    parts = parts_from_meta(meta)
    key = ThreadKey(system=system, thread=thread)
    writer = SessionWriter(store, key, owner="seed")
    await writer.aclaim()
    await writer.acommit(
        set_state=parts.state,
        set_usage=parts.usage,
        set_info=parts.info,
        set_runtime=parts.runtime,
        set_extra=parts.extra,
    )
    await writer.arelease()


async def thread_trace(store, system, thread):
    """Трасса субагентов треда в прежней форме записей."""
    from gc_core.threads.facade import Threads
    from gc_core.threads.models import ThreadKey

    key = ThreadKey(system=system, thread=thread)
    entries = await Threads(store).subagent_events(key)
    return [{"ts": e.ts, "seq": e.seq, **e.data} for e in entries]
