"""A real native child runs the same Runner, model/tools, input and HITL ports."""

import asyncio
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from live_support import temporal_cli
from gc_core.config_modules import AppConfig
from gc_core_temporalio.child_workflow import SubagentWorkflow
from gc_core_temporalio.contracts import RunRequest
from gc_core_temporalio.provider import (
    AgentProfile,
    StoreConfig,
    WorkerConfig,
)
from gc_core_temporalio.worker import build_runtime, worker_options
from gc_core_temporalio.workflow import AgentWorkflow
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
                **worker_options(config, native, stores)[0],
            ):
                handle = await env.client.start_workflow(
                    AgentWorkflow.run,
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
            replayer = Replayer(workflows=[AgentWorkflow, SubagentWorkflow])
            await replayer.replay_workflow(parent_history)
            await replayer.replay_workflow(child_history)
            assert len(requests) == expected_calls
    finally:
        await provider.aclose()
