"""Real process loss during human wait and after file publication, plus stop."""

import asyncio
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from gc_ua_temporalio.client import TemporalAgentClient
from gc_ua_temporalio.config import DEFAULT_TOOLS
from gc_ua_temporalio.contracts import SessionStart
from gc_ua_temporalio.control_contracts import HumanReply
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from gc_ua_temporalio.workflow import AgentSessionWorkflow
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer


def tool(name, arguments, index=1):
    return {
        "id": f"call-{index}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@pytest.fixture
def provider():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            data = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            requests.append(data)
            prompt = next(
                m["content"] for m in data["messages"] if m["role"] == "user"
            )
            if any(m["role"] == "tool" for m in data["messages"]):
                calls = [
                    tool(
                        "turn_finish",
                        {
                            "solution_type": "full_solution",
                            "task": "test",
                            "task_report": "done",
                            "handoff": "{}",
                        },
                        9,
                    )
                ]
            elif "stop" in prompt:
                calls = [
                    tool("exec_sleep", {"seconds": 120}),
                    tool(
                        "fs_write_file",
                        {"path": "must-not-exist.txt", "content": "bad"},
                        2,
                    ),
                ]
            elif "human" in prompt:
                calls = [
                    tool("turn_ask_human", {"question": "Какой цвет?"}),
                    tool(
                        "fs_write_file",
                        {"path": "must-not-exist.txt", "content": "stale"},
                        2,
                    ),
                ]
            else:
                path = "AGENTS.md" if "approval" in prompt else "result.txt"
                calls = [
                    tool(
                        "fs_write_file",
                        {"path": path, "content": "approved durable content"},
                    )
                ]
            body = json.dumps(
                {
                    "id": f"response-{len(requests)}",
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
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
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
    yield f"http://127.0.0.1:{server.server_port}/chat/completions", requests
    server.shutdown()
    server.server_close()
    thread.join()


async def until(handle, predicate, *, timeout=60):
    async with asyncio.timeout(timeout):
        while True:
            status = await handle.query("status")
            if predicate(status):
                return status
            if (await handle.describe()).status.name != "RUNNING":
                await handle.result()
                raise AssertionError("Workflow ended before checkpoint")
            await asyncio.sleep(0.1)


class Stand:
    def __init__(self, tmp_path, env, url):
        self.folder = tmp_path
        self.processes = []
        self.logs = []
        self.config = tmp_path / "runtime.json"
        self.config.write_text(
            json.dumps(
                {
                    "address": env.client.service_client.config.target_host,
                    "database": str(tmp_path / "threads.sqlite"),
                    "artifacts": str(tmp_path / "artifacts"),
                    "profiles": {
                        "demo": {
                            "workspace": str(tmp_path),
                            "model": "fixture",
                            "completions_url": url,
                            "max_steps": 8,
                            "tools": [
                                *DEFAULT_TOOLS,
                                "turn_ask_human",
                                "fs_write_file",
                            ],
                        }
                    },
                }
            )
        )

    def start(self, gate="none"):
        log = (self.folder / f"worker-{len(self.logs)}.log").open("w")
        self.logs.append(log)
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("worker_process.py")),
                str(self.config),
                gate,
                str(self.folder / "checkpoint"),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        return process

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        for log in self.logs:
            log.close()


@pytest.fixture
async def stand(tmp_path, provider):
    binary = shutil.which("temporal")
    if not binary:
        pytest.skip("Temporal CLI required")
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:
        harness = Stand(tmp_path, env, provider[0])
        try:
            yield harness, env.client
        finally:
            harness.close()


async def close_and_replay(_client, handle, tmp_path):
    await handle.signal("close_session")
    await handle.result()
    history = await handle.fetch_history()
    (tmp_path / "history.json").write_text(history.to_json())
    await Replayer(workflows=[AgentSessionWorkflow]).replay_workflow(history)


async def test_approval_survives_worker_loss_and_rejects_stale_reply(
    stand, tmp_path
):
    harness, client = stand
    (tmp_path / "AGENTS.md").write_text("original instructions")
    process = harness.start()
    handle = await TemporalAgentClient(client).start(
        SessionStart("approval", "approval test")
    )
    first = await until(handle, lambda s: s["phase"] == "waiting_human")
    assert not (await handle.describe()).raw_description.pending_activities
    assert (tmp_path / "AGENTS.md").read_text() == "original instructions"
    process.kill()
    process.wait(timeout=10)
    harness.start()
    restored = await handle.query("status")
    assert restored["pending_human"] == first["pending_human"]
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(
            AgentSessionWorkflow.answer_human,
            HumanReply("old-request", "stale", "allow_once"),
        )
    reply = HumanReply(
        first["pending_human"]["request_id"], "reply-1", "allow_once"
    )
    await handle.execute_update(AgentSessionWorkflow.answer_human, reply)
    await until(handle, lambda s: s["phase"] == "idle")
    assert (tmp_path / "AGENTS.md").read_text() == "approved durable content"
    modified = (tmp_path / "AGENTS.md").stat().st_mtime_ns
    await handle.execute_update(AgentSessionWorkflow.answer_human, reply)
    assert (tmp_path / "AGENTS.md").stat().st_mtime_ns == modified
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(
            AgentSessionWorkflow.answer_human,
            HumanReply(reply.request_id, "different", "allow_once"),
        )
    await close_and_replay(client, handle, tmp_path)


async def test_kill_after_file_write_before_receipt_reconciles_without_repeat(
    stand, tmp_path
):
    harness, client = stand
    process = harness.start("after_effect")
    handle = await TemporalAgentClient(client).start(
        SessionStart("effect", "effect test")
    )
    async with asyncio.timeout(60):
        while not (tmp_path / "checkpoint").exists():
            assert process.poll() is None
            if (await handle.describe()).status.name != "RUNNING":
                await handle.result()
            await asyncio.sleep(0.1)
    written = (tmp_path / "result.txt").stat().st_mtime_ns
    process.kill()
    process.wait(timeout=10)
    harness.start()
    await until(handle, lambda s: s["phase"] == "idle")
    assert (tmp_path / "result.txt").stat().st_mtime_ns == written
    assert (tmp_path / "result.txt").read_text() == "approved durable content"
    history = await handle.fetch_history()
    scheduled = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled.count("ua.effect") == 1
    assert scheduled.count("ua.reconcile") == 1
    await close_and_replay(client, handle, tmp_path)


async def test_stop_cancels_active_tool_and_pairs_unstarted_tail(
    stand, tmp_path
):
    harness, client = stand
    harness.start()
    handle = await TemporalAgentClient(client).start(
        SessionStart("stop", "stop test")
    )
    status = await until(handle, lambda s: s["tool_calls"] >= 1)
    await handle.execute_update(
        AgentSessionWorkflow.stop_turn, status["turn_id"]
    )
    await until(handle, lambda s: s["phase"] == "stopped")
    assert not (tmp_path / "must-not-exist.txt").exists()
    store = SQLiteThreadStore(tmp_path / "threads.sqlite")
    messages = store.load_messages("temporal", "stop")
    calls = {
        m["call_id"] for m in messages if m.get("type") == "function_call"
    }
    outputs = {
        m["call_id"]: json.loads(m["output"])
        for m in messages
        if m.get("type") == "function_call_output"
    }
    assert calls == outputs.keys()
    assert outputs["call-1"]["error"]["type"] == "user_stop"
    assert outputs["call-2"]["status"] == "not_started_due_to_user_stop"
    await close_and_replay(client, handle, tmp_path)


async def test_human_reply_discards_tools_generated_before_the_answer(
    stand, tmp_path
):
    harness, client = stand
    harness.start()
    handle = await TemporalAgentClient(client).start(
        SessionStart("human", "human test")
    )
    status = await until(handle, lambda s: s["phase"] == "waiting_human")
    await handle.execute_update(
        AgentSessionWorkflow.answer_human,
        HumanReply(status["pending_human"]["request_id"], "reply", "синий"),
    )
    await until(handle, lambda s: s["phase"] == "idle")
    assert not (tmp_path / "must-not-exist.txt").exists()
    await close_and_replay(client, handle, tmp_path)


async def test_stop_during_approval_rejects_late_answer_and_never_writes(
    stand, tmp_path
):
    harness, client = stand
    (tmp_path / "AGENTS.md").write_text("original")
    harness.start()
    handle = await TemporalAgentClient(client).start(
        SessionStart("approval-stop", "approval test")
    )
    status = await until(handle, lambda s: s["phase"] == "waiting_human")
    await handle.execute_update(
        AgentSessionWorkflow.stop_turn, status["turn_id"]
    )
    await until(handle, lambda s: s["phase"] == "stopped")
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(
            AgentSessionWorkflow.answer_human,
            HumanReply(
                status["pending_human"]["request_id"], "too-late", "allow_once"
            ),
        )
    assert (tmp_path / "AGENTS.md").read_text() == "original"
    await close_and_replay(client, handle, tmp_path)


async def test_stop_after_effect_reconciles_before_closing_turn(
    stand, tmp_path
):
    harness, client = stand
    harness.start("after_effect")
    handle = await TemporalAgentClient(client).start(
        SessionStart("effect-stop", "effect test")
    )
    async with asyncio.timeout(60):
        while not (tmp_path / "checkpoint").exists():
            await asyncio.sleep(0.1)
    modified = (tmp_path / "result.txt").stat().st_mtime_ns
    status = await handle.query("status")
    await handle.execute_update(
        AgentSessionWorkflow.stop_turn, status["turn_id"]
    )
    await until(handle, lambda s: s["phase"] == "stopped")
    assert (tmp_path / "result.txt").stat().st_mtime_ns == modified
    store = SQLiteThreadStore(tmp_path / "threads.sqlite")
    messages = store.load_messages("temporal", "effect-stop")
    outputs = [
        json.loads(m["output"])
        for m in messages
        if m.get("type") == "function_call_output"
    ]
    assert outputs[0]["ok"] is True
    await close_and_replay(client, handle, tmp_path)


async def test_unknown_waits_for_reconciliation_and_stop_does_not_hide_it(
    stand, tmp_path
):
    from gc_ua_temporalio.control_contracts import ReconcileRequest

    harness, client = stand
    process = harness.start("after_effect")
    handle = await TemporalAgentClient(client).start(
        SessionStart("unknown", "effect test")
    )
    async with asyncio.timeout(60):
        while not (tmp_path / "checkpoint").exists():
            await asyncio.sleep(0.1)
    process.kill()
    process.wait(timeout=10)
    (tmp_path / "result.txt").write_text("ambiguous intervening change")
    harness.start()
    status = await until(
        handle, lambda s: s["phase"] == "needs_reconciliation"
    )
    operation = status["reconciliation"]["operation_id"]
    await handle.execute_update(
        AgentSessionWorkflow.stop_turn, status["turn_id"]
    )
    assert (await handle.query("status"))["phase"] == "needs_reconciliation"
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(
            AgentSessionWorkflow.reconcile_operation,
            ReconcileRequest("wrong-operation", "wrong"),
        )
    # Operator restores the intended bytes, then requests inspection, not retry.
    (tmp_path / "result.txt").write_text("approved durable content")
    modified = (tmp_path / "result.txt").stat().st_mtime_ns
    request = ReconcileRequest(operation, "inspect-1")
    await handle.execute_update(
        AgentSessionWorkflow.reconcile_operation, request
    )
    await until(handle, lambda s: s["phase"] == "stopped")
    await handle.execute_update(
        AgentSessionWorkflow.reconcile_operation, request
    )
    assert (tmp_path / "result.txt").stat().st_mtime_ns == modified
    history = await handle.fetch_history()
    effects = [
        e
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
        and e.activity_task_scheduled_event_attributes.activity_type.name
        == "ua.effect"
    ]
    assert len(effects) == 1
    await close_and_replay(client, handle, tmp_path)
