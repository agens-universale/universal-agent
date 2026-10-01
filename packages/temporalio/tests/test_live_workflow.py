"""Real Temporal + HTTP provider + SIGKILL workers + offline sandbox replay."""

import asyncio
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from gc_core.usage.account import UsageAccount
from gc_ua_temporalio.client import TemporalAgentClient
from gc_ua_temporalio.contracts import SessionStart
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from gc_ua_temporalio.workflow import AgentSessionWorkflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer


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


@pytest.mark.asyncio
async def test_worker_crash_after_model_and_tools_then_offline_replay(
    tmp_path, provider
):
    binary = shutil.which("temporal")
    if not binary:
        pytest.skip("Install Temporal CLI to run the real-server acceptance")
    url, requests = provider
    (tmp_path / "example.txt").write_text("file from durable workspace")
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:
        config = {
            "address": env.client.service_client.config.target_host,
            "database": str(tmp_path / "threads.sqlite"),
            "artifacts": str(tmp_path / "artifacts"),
            "profiles": {
                "demo": {
                    "workspace": str(tmp_path),
                    "model": "scripted",
                    "completions_url": url,
                }
            },
        }
        config_path = tmp_path / "worker.json"
        config_path.write_text(json.dumps(config))
        worker_script = Path(__file__).with_name("worker_process.py")
        processes = []
        logs = []

        def start(gate):
            log = (tmp_path / f"worker-{len(processes)}.log").open("w")
            logs.append(log)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(worker_script),
                    str(config_path),
                    gate,
                    str(tmp_path / gate),
                ],
                stdout=log,
                stderr=log,
            )
            processes.append(process)
            return process

        try:
            first = start("before_tool")
            handle = await TemporalAgentClient(env.client).start(
                SessionStart(
                    "crash-test",
                    "Read example.txt and remember the result",
                    auto_close=True,
                )
            )
            await wait_marker(tmp_path / "before_tool", first, handle)
            first.kill()
            first.wait(timeout=10)
            assert len(requests) == 1
            second = start("after_tools")
            await wait_marker(tmp_path / "after_tools", second, handle)
            second.kill()
            second.wait(timeout=10)
            assert (
                len(requests) == 1
            )  # completed first model was not called again
            start("none")
            result = await asyncio.wait_for(handle.result(), 60)
            assert result["memory"]["knowledge"] == "durable fact"
            assert result["todo"] == [
                {"content": "verify recovery", "status": "completed"}
            ]
            assert result["steps"] == 2
            assert result["tool_calls"] == 4
            assert len(requests) == 2
            assert (
                UsageAccount.from_meta(result["usage"]).totals()["total"]
                == 100
            )
            executed = (
                (tmp_path / "executed-tools.txt").read_text().splitlines()
            )
            assert executed == [
                "session_memory",
                "session_todo",
                "fs_read_file",
                "turn_finish",
            ]
            history = await handle.fetch_history()
            (tmp_path / "history.json").write_text(history.to_json())
            store = SQLiteThreadStore(config["database"])
            messages = store.load_messages("temporal", "crash-test")
            outputs = [
                m for m in messages if m.get("type") == "function_call_output"
            ]
            assert [m["call_id"] for m in outputs] == [
                "memory",
                "todo",
                "read",
                "finish",
            ]
            assert len({m["id"] for m in messages}) == len(messages)
            assert "file from durable workspace" in next(
                m["output"] for m in outputs if m["call_id"] == "read"
            )
            assert (
                store.load_meta("temporal", "crash-test")["shared"][
                    "working_memory"
                ]["knowledge"]
                == "durable fact"
            )
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            for log in logs:
                log.close()
        # No activity workers exist now. Replay must require no provider/filesystem.
        before = len(requests)
        await Replayer(workflows=[AgentSessionWorkflow]).replay_workflow(
            history
        )
        assert len(requests) == before


async def test_local_and_portable_loop_have_the_same_observable_trace(
    tmp_path, provider
):
    import copy
    import logging
    from datetime import UTC, datetime

    from gc_core import processing_responses as pr
    from gc_core.execution.environment import execution_environment
    from gc_core.execution.local import LocalOperations
    from gc_core.execution.loop import AgentLoop
    from gc_core.execution.state import ExecutionState
    from gc_ua_temporalio.bindings import build_bindings
    from gc_ua_temporalio.config import TemporalRuntimeConfig
    from gc_ua_temporalio.contracts import BootstrapRequest
    from gc_ua_temporalio.runtime import WorkerRuntime

    url, _ = provider
    (tmp_path / "example.txt").write_text("file from durable workspace")
    config = TemporalRuntimeConfig(
        database=str(tmp_path / "db.sqlite"),
        artifacts=str(tmp_path / "artifacts"),
        profiles={
            "demo": {
                "workspace": str(tmp_path),
                "model": "scripted",
                "completions_url": url,
            }
        },
    )
    runtime = WorkerRuntime(config)
    snapshot = await runtime.bootstrap(
        BootstrapRequest(SessionStart("parity", "hi"), "owner")
    )
    traces = []
    states = []
    answers = []
    for portable in (False, True):
        state = ExecutionState.from_execution_snapshot(snapshot.state)
        ctx = copy.copy(runtime.contexts["demo"])
        ctx.operations = LocalOperations(ctx) if portable else None
        trace = []

        async def persist(messages, _trace=trace):
            _trace.extend(copy.deepcopy(messages))

        counter = iter(range(100))
        with execution_environment(
            new_id=lambda prefix, counter=counter: f"{prefix}-{next(counter)}",
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        ):
            messages = [
                {
                    "id": "user-0",
                    "role": "user",
                    "content": str(tmp_path / "example.txt"),
                }
            ]
            kwargs = dict(
                get_full_history_messages=None,
                persist_messages_callback=persist,
                pre_backend_compaction_hook=None,
                turn_end_compaction_hook=None,
                force_compact_hook=None,
                get_pending_messages=None,
                check_stop=None,
                session_meta=None,
                mark_finalizing=None,
                mark_finish_gate_checking=None,
            )
            if portable:
                answer = await AgentLoop(
                    state,
                    ctx,
                    bindings=build_bindings(logging.getLogger("parity")),
                    **kwargs,
                ).arun(messages)
            else:
                answer = await pr.arun_agent(messages, state, ctx, **kwargs)
        traces.append(trace)
        states.append(state.to_execution_snapshot())
        answers.append(answer)
    assert answers[0] == answers[1]
    assert traces[0] == traces[1]
    assert states[0] == states[1]
    # Golden behavior: one work batch, one finish, no duplicate calls or outputs.
    trace = traces[1]
    assert [m["name"] for m in trace if m.get("type") == "function_call"] == [
        "session_memory",
        "session_todo",
        "fs_read_file",
        "turn_finish",
    ]
    assert states[1]["data"]["steps"] == 2
    assert states[1]["data"]["tool_calls"] == 4
