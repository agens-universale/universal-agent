"""Multi-turn receipt, late input and Continue-As-New checks on a real server."""

import asyncio
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests
from gc_ua_temporalio.client import TemporalAgentClient
from gc_ua_temporalio.contracts import SessionStart
from gc_ua_temporalio.session_contracts import UserMessage
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from gc_ua_temporalio.workflow import AgentSessionWorkflow
from temporalio.client import (
    WithStartWorkflowOperation,
    WorkflowUpdateFailedError,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_live_controls import Stand, tool, until


@pytest.fixture
def session_provider():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            requests.append(data)
            if not data.get("tools"):
                self._respond(
                    {
                        "role": "assistant",
                        "content": valid_summary_xml_for_tests(
                            seed="durable summary"
                        ),
                    },
                    "stop",
                )
                return
            last_user = max(
                i
                for i, m in enumerate(data["messages"])
                if m["role"] == "user"
            )
            prompt = data["messages"][last_user]["content"]
            has_result = any(
                m["role"] == "tool" for m in data["messages"][last_user:]
            )
            index = len(requests)
            if not has_result:
                calls = (
                    [tool("exec_sleep", {"seconds": 2}, index)]
                    if "wait" in prompt
                    else [
                        tool(
                            "session_memory",
                            {
                                "action": "append",
                                "register": "knowledge",
                                "content": prompt,
                            },
                            index,
                        )
                    ]
                )
            else:
                calls = [
                    tool(
                        "turn_finish",
                        {
                            "solution_type": "full_solution",
                            "task": prompt,
                            "task_report": "done",
                            "handoff": "{}",
                        },
                        index,
                    )
                ]
            self._respond(
                {"role": "assistant", "tool_calls": calls}, "tool_calls"
            )

        def _respond(self, message, finish_reason):
            index = len(requests)
            body = json.dumps(
                {
                    "id": f"response-{index}",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": finish_reason,
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


async def test_continue_as_new_deduplicates_messages_and_keeps_memory(
    tmp_path, session_provider
):
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=shutil.which("temporal")
    ) as env:
        stand = Stand(tmp_path, env, session_provider[0])
        stand.start()
        try:
            client = TemporalAgentClient(env.client)
            handle = await client.start(
                SessionStart("multiple", "first fact", continue_after_turns=1)
            )
            original = handle.first_execution_run_id
            await until(
                handle,
                lambda s: (
                    s.get("run_id") != original
                    and len(s.get("turns", [])) == 1
                ),
            )
            first_history = await env.client.get_workflow_handle(
                handle.id, run_id=original
            ).fetch_history()
            replies = await asyncio.gather(
                *[
                    client.submit_message(
                        handle.id, UserMessage("message-2", "second fact")
                    )
                    for _ in range(3)
                ]
            )
            assert all(r["message_id"] == "message-2" for r in replies)
            await until(
                handle,
                lambda s: (
                    len(s.get("turns", [])) == 2 and s["phase"] == "idle"
                ),
            )
            result = await client.submit_message(
                handle.id, UserMessage("message-2", "second fact")
            )
            assert result["disposition"] == "PROCESSED"
            with pytest.raises(WorkflowUpdateFailedError):
                await client.submit_message(
                    handle.id, UserMessage("message-2", "different")
                )
            await client.close_session(handle.id)
            result = await handle.result()
            assert result["memory"]["knowledge"].count("first fact") == 1
            assert result["memory"]["knowledge"].count("second fact") == 1
            assert len(session_provider[1]) == 4
            store = SQLiteThreadStore(str(tmp_path / "threads.sqlite"))
            messages = store.load_thread_state("temporal", "multiple").messages
            assert sum(m.get("id") == "user:message-2" for m in messages) == 1
            await Replayer(workflows=[AgentSessionWorkflow]).replay_workflow(
                first_history
            )
        finally:
            stand.close()


async def test_message_update_can_arrive_before_workflow_run(
    tmp_path, session_provider
):
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=shutil.which("temporal")
    ) as env:
        stand = Stand(tmp_path, env, session_provider[0])
        stand.start()
        try:
            operation = WithStartWorkflowOperation(
                AgentSessionWorkflow.run,
                SessionStart(
                    "early", "first fact", initial_message_id="early-1"
                ),
                id="early-update",
                task_queue="ua-workflow",
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
            receipt = await env.client.execute_update_with_start_workflow(
                AgentSessionWorkflow.submit_message,
                UserMessage("early-1", "first fact"),
                start_workflow_operation=operation,
            )
            assert receipt["message_id"] == "early-1"
            handle = await operation.workflow_handle()
            await until(
                handle, lambda s: s["phase"] == "idle" and s.get("turns")
            )
            await TemporalAgentClient(env.client).close_session(handle.id)
            result = await handle.result()
            assert result["memory"]["knowledge"].count("first fact") == 1
            assert len(session_provider[1]) == 2
            await Replayer(workflows=[AgentSessionWorkflow]).replay_workflow(
                await handle.fetch_history()
            )
        finally:
            stand.close()


async def test_summary_restart_applies_patch_once_and_preserves_memory(
    tmp_path, session_provider
):
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=shutil.which("temporal")
    ) as env:
        stand = Stand(tmp_path, env, session_provider[0])
        config = json.loads(stand.config.read_text())
        config["profiles"]["demo"]["summary_trigger_chars"] = 6000
        stand.config.write_text(json.dumps(config))
        process = stand.start("after_summary")
        prompt = "remember: " + "durable fact " * 330
        try:
            client = TemporalAgentClient(env.client)
            handle = await client.start(SessionStart("summary", prompt))
            async with asyncio.timeout(40):
                while not (tmp_path / "checkpoint").exists():
                    await asyncio.sleep(0.1)
            before = len(session_provider[1])
            assert any(not r.get("tools") for r in session_provider[1])
            process.kill()
            process.wait()
            stand.start()
            await until(
                handle, lambda s: s["phase"] == "idle" and s.get("turns")
            )
            store = SQLiteThreadStore(str(tmp_path / "threads.sqlite"))
            assert store.load_meta("temporal", "summary").get("summary_traces")
            await client.close_session(handle.id)
            result = await handle.result()
            assert result["memory"]["knowledge"].count("remember:") == 1
            # Re-running preparation consumes saved model answers, not new requests.
            assert len(session_provider[1]) >= before
            history = await handle.fetch_history()
            await Replayer(workflows=[AgentSessionWorkflow]).replay_workflow(
                history
            )
        finally:
            stand.close()


async def test_pending_input_and_worker_restart_keep_both_user_messages(
    tmp_path, session_provider
):
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=shutil.which("temporal")
    ) as env:
        stand = Stand(tmp_path, env, session_provider[0])
        process = stand.start("before_tool")
        try:
            client = TemporalAgentClient(env.client)
            handle = await client.start(
                SessionStart("pending", "wait for next message")
            )
            async with asyncio.timeout(30):
                while not (tmp_path / "checkpoint").exists():
                    await asyncio.sleep(0.1)
            await client.submit_message(
                handle.id, UserMessage("tail-1", "late fact")
            )
            process.kill()
            process.wait()
            stand.start()
            await until(
                handle, lambda s: s["phase"] == "idle" and s.get("turns")
            )
            await client.close_session(handle.id)
            result = await handle.result()
            assert "late fact" in result["memory"]["knowledge"]
            store = SQLiteThreadStore(str(tmp_path / "threads.sqlite"))
            messages = store.load_thread_state("temporal", "pending").messages
            assert (
                sum(
                    m.get("content") == "wait for next message"
                    for m in messages
                )
                == 1
            )
            assert sum(m.get("id") == "user:tail-1" for m in messages) == 1
            await Replayer(workflows=[AgentSessionWorkflow]).replay_workflow(
                await handle.fetch_history()
            )
        finally:
            stand.close()
