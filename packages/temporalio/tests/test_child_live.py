"""A real native child runs the same Runner, model/tools, input and HITL ports."""

import asyncio
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from live_support import temporal_cli, worker_kwargs
from gc_core.config_modules import AppConfig
from gc_core_temporalio.child_workflow import UniversalSubagentWorkflow
from gc_core_temporalio.contracts import RunRequest
from gc_core_temporalio.provider import (
    AgentProfile,
    StoreConfig,
    WorkerConfig,
)
from gc_core_temporalio.worker import build_runtime
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from gc_core_temporalio.contracts import UserMessage
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker


def tool(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


@pytest.fixture
def child_provider(tmp_path, tool_name):
    requests = []
    child_calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            request["_endpoint"] = self.path
            requests.append(request)
            if self.path == "/dead":
                body = json.dumps(
                    {
                        "error": {
                            "message": "model not found",
                            "type": "invalid_request_error",
                            "code": "model_not_found",
                        }
                    }
                ).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            child = any(
                (
                    "name: subagent_worker" in str(message.get("content"))
                    or "NATIVE CUSTOM CHILD ROLE"
                    in str(message.get("content"))
                )
                for message in request["messages"]
                if message["role"] == "system"
            )
            outputs = [m for m in request["messages"] if m["role"] == "tool"]
            if child:
                child_calls.append(request)
            if child and len(child_calls) == 1:
                calls = [
                    tool(
                        "ask",
                        "turn_ask_human",
                        {"question": "Child needs a detail"},
                    )
                ]
            elif child and len(child_calls) == 2:
                calls = [
                    tool(
                        "memory",
                        "session_memory",
                        {
                            "action": "append",
                            "register": "knowledge",
                            "content": "child fact",
                        },
                    ),
                    tool(
                        "todo",
                        "session_todo",
                        {
                            "todos": [
                                {
                                    "content": "child task",
                                    "status": "completed",
                                }
                            ]
                        },
                    ),
                    tool(
                        "read",
                        "fs_read_file",
                        {"path": str(tmp_path / "example.txt")},
                    ),
                ]
            elif not child and not outputs:
                calls = [
                    tool(
                        "child-call",
                        tool_name,
                        {"task": "Read the file", "reason": "Delegate"},
                    )
                ]
            else:
                calls = [
                    tool(
                        "finish",
                        "turn_finish",
                        {
                            "solution_type": "full_solution",
                            "task": "demo",
                            "task_report": "Child done"
                            if child
                            else "Parent done",
                            "handoff": "{}",
                        },
                    )
                ]
            body = json.dumps(
                {
                    "id": f"response-{len(requests)}",
                    "model": "scripted",
                    "object": "chat.completion",
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
            ).encode()
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
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "tool_name,stop,fallback,big",
    [
        ("subagent_worker", False, False, False),
        ("subagent_worker", True, False, False),
        ("subagent_custom", False, False, False),
        ("subagent_worker", False, True, False),
        # Тред родителя ~2,2 МБ в истории: состояние родителя целиком во
        # входе субагента было бы больше записи (вечный сбой задачи).
        ("subagent_worker", False, False, True),
    ],
)
async def test_native_child_model_tools_input_human_and_replay(
    tmp_path, monkeypatch, child_provider, stop, tool_name, fallback, big
):
    binary = temporal_cli()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "example.txt").write_text("native child file")
    url, requests = child_provider
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "custom.md").write_text(
        "---\nname: subagent_custom\ndescription: Custom child\n"
        "tools: [turn_finish, session_read_stored_tool_output, session_memory, session_todo, fs_read_file, turn_ask_human]\n"
        "stop_tools: []\n---\nNATIVE CUSTOM CHILD ROLE\n"
    )
    core = AppConfig.model_validate(
        {
            "backend": {
                "api_mode": "chat_completions",
                "completions_url": url,
                "stream": False,
                "chat_completion_kwargs": {"stream": False},
            },
            "agent": {
                "model": "scripted",
                "use_local_subagents": True,
                "subagents_dir": str(agents),
                "max_steps": 10,
                "strict_turn_finish_validation": False,
            },
            "logging": {"save_logs": False, "console_trace": False},
            "tui": {"text_answer_mode": "deny"},
        }
    )
    if fallback:
        model_config = core.model_dump()
        model_config.update(
            {
                "models": {
                    "active": {
                        "model": "active-model",
                        "url": url,
                        "token": "",
                    },
                    "pin": {
                        "model": "dead-model",
                        "token": "",
                        "url": url.replace("/v1/chat/completions", "/dead"),
                    },
                },
                "slots": {"default": "active"},
                "assign": {"subagents": {tool_name: "pin"}},
            }
        )
        core = AppConfig.model_validate(model_config)
    config = WorkerConfig(
        human_input=True,  # вопрос ребёнка человеку
        workflow_queue="child-test",
        model_queue="child-test",
        resource_queue="child-test",
        state_dir=str(tmp_path),
        thread_stores={"files": StoreConfig(path=str(tmp_path / "threads"))},
        profiles={"test": AgentProfile(workspace=str(tmp_path), core=core)},
    )
    if big:
        from gc_core.thread_store_file_durable import DurableFileThreadStore

        DurableFileThreadStore(str(tmp_path / "threads")).append_messages(
            "test",
            "thread",
            [
                {
                    "id": f"old{i}",
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"{i}:" + "ж" * 12_000,
                }
                for i in range(30)
            ],
        )
    provider, native, stores = build_runtime(config)
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            async with Worker(
                env.client,
                **worker_kwargs(config, native, stores),
            ):
                handle = await env.client.start_workflow(
                    UniversalAgentWorkflow.run,
                    RunRequest(
                        "test",
                        "files",
                        "test",
                        "thread",
                        ["Delegate to worker"],
                        "child-test",
                        "child-test",
                    ),
                    id="native-child-test",
                    task_queue="child-test",
                )

                async def wait_human():
                    while True:
                        status = await handle.query("status")
                        if status.get("pending_human"):
                            return status
                        if status["phase"] in ("completed", "stopped"):
                            raise AssertionError(
                                f"Run finished without child HITL: {status}"
                            )
                        await asyncio.sleep(0.05)

                status = await asyncio.wait_for(wait_human(), 30)
                assert status["active_child"]
                if stop:
                    await handle.signal("stop")
                else:
                    await handle.execute_update(
                        "submit_message",
                        UserMessage(
                            "extra", "Also report the exact file text"
                        ),
                    )
                    await handle.execute_update(
                        "answer_human",
                        args=[
                            status["pending_human"]["request_id"],
                            "Proceed",
                        ],
                    )
                result = await asyncio.wait_for(handle.result(), 30)
                final_status = await handle.query("status")
                parent_history = await handle.fetch_history()
                child_history = await env.client.get_workflow_handle(
                    status["active_child"]
                ).fetch_history()
            expected_calls = 2 if stop else 5 + int(fallback)
            assert len(requests) == expected_calls
            if fallback:
                assert [request["model"] for request in requests] == [
                    "active-model",
                    "dead-model",
                    "active-model",
                    "active-model",
                    "active-model",
                    "active-model",
                ]
                assert (
                    sum(
                        request["_endpoint"] == "/dead" for request in requests
                    )
                    == 1
                )
            if tool_name == "subagent_custom":
                names = {
                    entry.get("function", entry)["name"]
                    for entry in requests[1]["tools"]
                }
                assert names == {
                    "turn_finish",
                    "session_read_stored_tool_output",
                    "session_memory",
                    "session_todo",
                    "fs_read_file",
                    "turn_ask_human",
                }
            meta = provider.stores["files"].load_meta("test", "thread")
            if stop:
                assert final_status["phase"] == "stopped"
            else:
                assert "Parent done" in str(result["answer"])
                assert any(
                    "Also report the exact file text"
                    in str(request["messages"])
                    for request in requests
                )
                assert (
                    meta["shared"]["working_memory"]["knowledge"]
                    == "child fact"
                )
            assert not meta["shared"].get("session_todo")
            assert meta["token_usage"]["by_category"]["subagent"]["calls"] == (
                1 if stop else 3
            )
            trace = provider.stores["files"].read_subagent_trace(
                "test", "thread"
            )
            assert any(record["kind"] == "finished" for record in trace)
            assert all(
                record["parent_call_id"] == "child-call" for record in trace
            )
            if not stop:
                assert "native child file" in str(trace)
            replayer = Replayer(
                workflows=[UniversalAgentWorkflow, UniversalSubagentWorkflow]
            )
            await replayer.replay_workflow(parent_history)
            await replayer.replay_workflow(child_history)
            assert len(requests) == expected_calls
    finally:
        await provider.aclose()


@pytest.fixture
def auto_child_provider(tmp_path):
    """Детерминированные ответы реальному HTTP-транспорту родителя и ребёнка."""
    child_requests = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            requests.append(request)
            child = any(
                "name: subagent_worker" in str(m.get("content", ""))
                for m in request["messages"]
                if m["role"] == "system"
            )
            if child:
                child_requests.append(request)
            if child and len(child_requests) <= 3:
                n = len(child_requests)
                calls = [
                    tool(
                        f"read-{n}",
                        "fs_read_file",
                        {"path": str(tmp_path / f"file-{n}.txt")},
                    )
                ]
            elif not child and len(requests) == 1:
                calls = [
                    tool(
                        "child-call",
                        "subagent_worker",
                        {"task": "Read three files", "reason": "Delegate"},
                    )
                ]
            else:
                calls = [
                    tool(
                        "finish",
                        "turn_finish",
                        {
                            "solution_type": "full_solution",
                            "task": "demo",
                            "task_report": "Child done"
                            if child
                            else "Parent done",
                            "handoff": "{}",
                        },
                    )
                ]
            body = json.dumps(
                {
                    "id": f"response-{len(requests)}",
                    "model": request["model"],
                    "object": "chat.completion",
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
            ).encode()
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
            child_requests,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("legacy", [False, True], ids=["auto", "old_history"])
async def test_native_unbound_child_auto_and_old_history_replay(
    tmp_path, monkeypatch, auto_child_provider, legacy
):
    """Ребёнок чередует модели; история без patch marker сохраняет ручной путь."""
    from gc_core_temporalio.child_contracts import ChildRequest

    binary = temporal_cli()
    monkeypatch.chdir(tmp_path)
    for n in range(1, 4):
        (tmp_path / f"file-{n}.txt").write_text(f"file {n}")
    url, child_requests = auto_child_provider
    core = AppConfig.model_validate(
        {
            "level": "auto",
            "models": {
                "fast": {"model": "fast-model", "url": url, "token": ""},
                "mid": {"model": "mid-model", "url": url, "token": ""},
            },
            "slots": {"light": "fast", "default": "mid"},
            "backend": {
                "api_mode": "chat_completions",
                "stream": False,
                "chat_completion_kwargs": {"stream": False},
            },
            "agent": {
                "use_local_subagents": True,
                "max_steps": 10,
                "strict_turn_finish_validation": False,
                "auto_routing_two_level": {
                    "strong_steps": 2,
                    "weak_steps": 10,
                },
            },
            "logging": {"save_logs": False, "console_trace": False},
            "tui": {"text_answer_mode": "deny"},
        }
    )
    config = WorkerConfig(
        workflow_queue="auto-child",
        model_queue="auto-child",
        resource_queue="auto-child",
        state_dir=str(tmp_path),
        thread_stores={"files": StoreConfig(path=str(tmp_path / "threads"))},
        profiles={"test": AgentProfile(workspace=str(tmp_path), core=core)},
    )
    provider, native, stores = build_runtime(config)
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            options = worker_kwargs(config, native, stores)
            if legacy:
                # SDK записывает настоящую историю старого пути без patch marker;
                # затем новый код Replayer сам выбирает прежнее поведение.
                options["patch_activation_callback"] = lambda _patch: False
            async with Worker(env.client, **options):
                handle = await env.client.start_workflow(
                    UniversalAgentWorkflow.run,
                    RunRequest(
                        "test",
                        "files",
                        "test",
                        "thread",
                        ["Delegate three reads"],
                        "auto-child",
                        "auto-child",
                    ),
                    id="unbound-auto-child",
                    task_queue="auto-child",
                )
                result = await asyncio.wait_for(handle.result(), 45)
                parent_history = await handle.fetch_history()
                event = next(
                    e
                    for e in parent_history.events
                    if e.HasField(
                        "start_child_workflow_execution_initiated_event_attributes"
                    )
                )
                attrs = event.start_child_workflow_execution_initiated_event_attributes
                (child_request,) = await env.client.data_converter.decode(
                    attrs.input.payloads, [ChildRequest]
                )
                child_history = await env.client.get_workflow_handle(
                    attrs.workflow_id
                ).fetch_history()
            assert "Parent done" in str(result["answer"])
            if legacy:
                assert "frozen_live_level" not in child_request.child_context
                assert [r["model"] for r in child_requests] == [
                    "mid-model"
                ] * 4
            else:
                assert (
                    child_request.child_context["frozen_live_level"] == "auto"
                )
                assert child_request.child_context["frozen_level"] == "default"
                assert [r["model"] for r in child_requests] == [
                    "mid-model",
                    "mid-model",
                    "fast-model",
                    "fast-model",
                    "mid-model",
                ]
            replayer = Replayer(
                workflows=[UniversalAgentWorkflow, UniversalSubagentWorkflow]
            )
            await replayer.replay_workflow(parent_history)
            await replayer.replay_workflow(child_history)
    finally:
        await provider.aclose()


@pytest.fixture
def independent_child_provider(tmp_path):
    requests, child_requests, cards = [], [], []
    control = {"fail_weak": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            request["_endpoint"] = self.path
            requests.append(request)
            card = None
            try:
                candidate = json.loads(request["messages"][-1]["content"])
                if (
                    isinstance(candidate, dict)
                    and "earlier_history_compacted" in candidate
                ):
                    card = candidate
            except (ValueError, TypeError, KeyError):
                pass
            child = any(
                "name: subagent_worker" in str(m.get("content", ""))
                for m in request["messages"]
                if m["role"] == "system"
            )
            calls = []
            content = None
            if card is not None:
                cards.append(card)
                score = 2 if "CHILD_REVIEW_TASK" in card["request"] else 4
                content = json.dumps(
                    {"score": score, "reason": "scripted difficulty"}
                )
            elif child:
                child_requests.append(request)
                if control["fail_weak"] and self.path == "/fast":
                    body = b'{"error":{"message":"provider unavailable"}}'
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                n = len(child_requests)
                if n <= 2:
                    calls = [
                        tool(
                            f"read-{n}",
                            "fs_read_file",
                            {"path": str(tmp_path / f"file-{n}.txt")},
                        )
                    ]
                elif not any(
                    "child-write" in str(m.get("tool_call_id", ""))
                    for m in request["messages"]
                    if m["role"] == "tool"
                ):
                    calls = [
                        tool(
                            "child-write",
                            "fs_write_file",
                            {
                                "path": str(tmp_path / "child-result.txt"),
                                "content": "child result",
                            },
                        )
                    ]
                else:
                    calls = [
                        tool(
                            "child-finish",
                            "turn_finish",
                            {
                                "solution_type": "full_solution",
                                "task": "child",
                                "task_report": "Child done",
                                "handoff": "{}",
                            },
                        )
                    ]
            elif not any(m["role"] == "tool" for m in request["messages"]):
                calls = [
                    tool(
                        "child-call",
                        "subagent_worker",
                        {
                            "task": "CHILD_REVIEW_TASK: read two files and write child-result.txt",
                            "reason": "Delegate",
                        },
                    )
                ]
            else:
                calls = [
                    tool(
                        "parent-finish",
                        "turn_finish",
                        {
                            "solution_type": "full_solution",
                            "task": "parent",
                            "task_report": "Parent done",
                            "handoff": "{}",
                        },
                    )
                ]
            message = {"role": "assistant", "content": content}
            if calls:
                message["tool_calls"] = calls
            body = json.dumps(
                {
                    "id": f"r-{len(requests)}",
                    "model": request["model"],
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if calls else "stop",
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
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}",
            requests,
            child_requests,
            cards,
            control,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "version", ["new", "old_auto", "old_manual", "private", "fallback"]
)
async def test_independent_child_pair_and_recorded_history_replay(
    tmp_path, monkeypatch, independent_child_provider, version
):
    from gc_core_temporalio.child_contracts import ChildRequest, PreparedChild

    binary = temporal_cli()
    monkeypatch.chdir(tmp_path)
    for n in (1, 2):
        (tmp_path / f"file-{n}.txt").write_text(f"file {n}")
    url, requests, child_requests, cards, control = independent_child_provider
    control["fail_weak"] = version == "fallback"
    private = version == "private"
    models = {
        alias: {
            "model": "shared-model" if private else f"{alias}-model",
            "url": f"{url}/{alias}",
            "token": f"private-{alias}" if private else "",
        }
        for alias in ("fast", "mid", "big")
    }
    core = AppConfig.model_validate(
        {
            "level": "auto",
            "models": models,
            "slots": {"light": "fast", "default": "mid", "max": "big"},
            "backend": {
                "api_mode": "chat_completions",
                "stream": False,
                "chat_completion_kwargs": {"stream": False},
            },
            "agent": {
                "use_local_subagents": True,
                "max_steps": 12,
                "strict_turn_finish_validation": False,
                "auto_routing_two_level": {
                    "strong_steps": 2,
                    "weak_steps": 10,
                },
            },
            "logging": {"save_logs": False, "console_trace": False},
            "tui": {"text_answer_mode": "deny"},
        }
    )
    config = WorkerConfig(
        workflow_queue="independent",
        model_queue="independent",
        resource_queue="independent",
        state_dir=str(tmp_path),
        thread_stores={"files": StoreConfig(path=str(tmp_path / "threads"))},
        profiles={"test": AgentProfile(workspace=str(tmp_path), core=core)},
    )
    provider, native, stores = build_runtime(config)
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            options = worker_kwargs(config, native, stores)
            if version == "old_auto":
                options["patch_activation_callback"] = lambda item: (
                    item.patch_id != "subagent-independent-auto-v1"
                )
            elif version == "old_manual":
                options["patch_activation_callback"] = lambda name: False
            async with Worker(env.client, **options):
                handle = await env.client.start_workflow(
                    UniversalAgentWorkflow.run,
                    RunRequest(
                        "test",
                        "files",
                        "test",
                        "thread",
                        ["Delegate the work"],
                        "independent",
                        "independent",
                    ),
                    id="independent-child",
                    task_queue="independent",
                )
                result = await asyncio.wait_for(handle.result(), 55)
                parent_history = await handle.fetch_history()
                event = next(
                    e
                    for e in parent_history.events
                    if e.HasField(
                        "start_child_workflow_execution_initiated_event_attributes"
                    )
                )
                attrs = event.start_child_workflow_execution_initiated_event_attributes
                (request,) = await env.client.data_converter.decode(
                    attrs.input.payloads, [ChildRequest]
                )
                child_history = await env.client.get_workflow_handle(
                    attrs.workflow_id
                ).fetch_history()
            assert "Parent done" in str(result["answer"])
            assert (
                tmp_path / "child-result.txt"
            ).read_text() == "child result"
            child_cards = [
                c for c in cards if "CHILD_REVIEW_TASK" in c["request"]
            ]
            if version in ("new", "private", "fallback"):
                assert len(child_cards) == 1
                assert request.child_context["independent_auto"] is True
                assert request.child_context["frozen_live_level"] == "auto"
                endpoints = [r["_endpoint"] for r in child_requests]
                if version == "fallback":
                    # Слабая недоступна (сколько бы ни было ретраев транспорта) —
                    # тот же такт повторяется ближайшим живым уровнем вверх (mid),
                    # завершение — в серии сильной; модель родителя не нужна.
                    fails = endpoints.count("/fast")
                    assert fails >= 1
                    assert endpoints == ["/mid", "/mid"] + [
                        "/fast"
                    ] * fails + ["/mid", "/mid"]
                else:
                    assert endpoints == [
                        "/mid",
                        "/mid",
                        "/fast",
                        "/mid",
                        "/mid",
                    ]
                # Decode the recorded child preparation, not fresh worker settings.
                completion = next(
                    e
                    for e in child_history.events
                    if e.HasField("activity_task_completed_event_attributes")
                )
                (prepared,) = await env.client.data_converter.decode(
                    completion.activity_task_completed_event_attributes.result.payloads,
                    [PreparedChild],
                )
                assert prepared.request_equivalence is not None
                assert (
                    prepared.request_equivalence.context_id
                    == prepared.context.context.run_id
                )
                assert request.parent_context.request_equivalence is not None
                assert (
                    request.parent_context.request_equivalence.context_id
                    == request.parent_context.context.run_id
                )
                assert (
                    request.parent_context.request_equivalence.context_id
                    != prepared.request_equivalence.context_id
                )
                from dataclasses import asdict

                recorded = json.dumps(asdict(prepared), default=str)
                assert "private-fast" not in recorded
                assert "private-mid" not in recorded
                assert "private-big" not in recorded
            elif version == "old_auto":
                assert child_cards == []
                assert "independent_auto" not in request.child_context
                assert [r["_endpoint"] for r in child_requests] == [
                    "/big",
                    "/big",
                    "/mid",
                    "/big",
                    "/big",
                ]
            else:
                assert child_cards == []
                assert "frozen_live_level" not in request.child_context
                assert [r["_endpoint"] for r in child_requests] == ["/big"] * 4
            (tmp_path / f"parent-history-{version}.json").write_text(
                parent_history.to_json()
            )
            (tmp_path / f"child-history-{version}.json").write_text(
                child_history.to_json()
            )
            if version in ("old_auto", "old_manual"):
                from temporalio.api.common.v1 import Payload

                completion = next(
                    e
                    for e in child_history.events
                    if e.HasField("activity_task_completed_event_attributes")
                )
                old_payload = Payload()
                old_payload.CopyFrom(
                    completion.activity_task_completed_event_attributes.result.payloads[
                        0
                    ]
                )
                old_body = json.loads(old_payload.data)
                old_body.pop("request_equivalence", None)
                old_payload.data = json.dumps(old_body).encode()
                (old_prepared,) = await env.client.data_converter.decode(
                    [old_payload], [PreparedChild]
                )
                assert old_prepared.request_equivalence is None
            before = len(requests)
            replayer = Replayer(
                workflows=[UniversalAgentWorkflow, UniversalSubagentWorkflow]
            )
            await replayer.replay_workflow(parent_history)
            await replayer.replay_workflow(child_history)
            assert len(requests) == before
    finally:
        await provider.aclose()
