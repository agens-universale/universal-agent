"""Actual native resource hosts cannot reconnect after process loss."""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from gc_core.thread_store_file import FileThreadStore
from gc_ua_temporalio.native_client import NativeAgentClient
from gc_ua_temporalio.native_contracts import NativeRunRequest
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from gc_ua_temporalio.storage.effects import EffectLedger
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from native_restart_interceptor import RestartInterceptor
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_live_workflow import call, wait_marker
from test_native_workflow_controls import scripted_http  # noqa: F401


@pytest.mark.parametrize("resource", ["mcp", "background"])
async def test_actual_resource_host_sigkill_never_reconnects_or_repeats(
    tmp_path,
    scripted_http,  # noqa: F811
    resource,
):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
    url, requests, script = scripted_http
    audit = tmp_path / "effects.jsonl"
    core = {
        "backend": {
            "api_mode": "chat_completions",
            "completions_url": url,
            "chat_completion_kwargs": {"stream": False},
        },
        "agent": {
            "model": "scripted",
            "max_steps": 5,
            "strict_turn_finish_validation": False,
        },
        "tui": {"text_answer_mode": "deny"},
        "logging": {"save_logs": False, "console_trace": False},
    }
    if resource == "mcp":
        core["mcp"] = {
            "servers": {
                "counter": {
                    "transport": "stdio",
                    "persistent_session": True,
                    "command": sys.executable,
                    "args": [
                        str(Path(__file__).with_name("mcp_fixture_server.py")),
                        "stdio",
                        "0",
                        str(audit),
                    ],
                }
            }
        }
        name, arguments = "mcp_counter_increment", {"delay": 0}
    else:
        name, arguments = (
            "exec_command",
            {
                "command": "echo effect >> effects.jsonl",
                "cwd": str(tmp_path),
                "run_in_background": True,
            },
        )
    script.append([call("first", name, arguments)])
    judge = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "verdict": "safe",
                            "reasons": [],
                            "confidence": 1,
                        }
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }
    if resource == "background":
        script.append(judge)
    script.append([call("second", name, arguments)])
    if resource == "background":
        script.append(judge)
    processes, logs = [], []
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:
        config = {
            "address": env.client.service_client.config.target_host,
            "receipts_database": str(tmp_path / "receipts.sqlite"),
            "thread_stores": {"files": {"path": str(tmp_path / "threads")}},
            "profiles": {
                "test": {
                    "revision": "v1",
                    "workspace": str(tmp_path),
                    "core": core,
                }
            },
        }
        config_path = tmp_path / "worker.json"
        config_path.write_text(json.dumps(config))
        marker_path = tmp_path / "checkpoint.json"

        def start(checkpoint):
            log = (tmp_path / f"worker-{len(processes)}.log").open("w")
            logs.append(log)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("native_worker_process.py")),
                    str(config_path),
                    checkpoint,
                    str(marker_path),
                ],
                cwd=tmp_path,
                stdout=log,
                stderr=log,
            )
            processes.append(process)
            return process

        try:
            first = start("after_tool_receipt")
            handle = await NativeAgentClient(env.client).start(
                NativeRunRequest(
                    "test",
                    "v1",
                    "files",
                    "test",
                    "lost",
                    ["Execute the two requested effects."],
                    "ua-native-resource-local",
                    "ua-native-model",
                ),
                workflow_id="native-resource-lost",
            )
            await wait_marker(marker_path, first, handle)
            async with asyncio.timeout(5):
                while not audit.exists():
                    await asyncio.sleep(0.02)
            before = audit.read_text()
            assert len(before.splitlines()) == (2 if resource == "mcp" else 1)
            marker = json.loads(marker_path.read_text())["value"]
            first.kill()
            first.wait(timeout=10)
            assert len(requests) == (1 if resource == "mcp" else 2)
            start("none")
            with pytest.raises(WorkflowFailureError) as failed:
                await asyncio.wait_for(handle.result(), 45)
            assert "ResourceOwnerLost" in str(failed.value.cause)
            assert audit.read_text() == before
            assert len(requests) == (1 if resource == "mcp" else 4)
            ledger = EffectLedger(
                SQLiteThreadStore(config["receipts_database"])
            )
            original = marker["call"]
            receipt = ledger.get(
                original["owner_id"], original["operation_id"]
            )
            assert receipt["status"] == "SUCCEEDED"
            messages = FileThreadStore(tmp_path / "threads").load_messages(
                "test", "lost"
            )
            assert len({m["id"] for m in messages}) == len(messages)
            history = await handle.fetch_history()
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
        await Replayer(
            workflows=[NativeAgentWorkflow],
            interceptors=[RestartInterceptor()],
            workflow_failure_exception_types=[Exception],
        ).replay_workflow(history)
        assert audit.read_text() == before


async def test_native_cancellation_during_actual_mcp_teardown(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    from gc_core.execution.resource_host import _close_mcp_owner
    from test_native_workflow_controls import finish, request, runtime

    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [[call("one", "mcp_counter_increment", {"delay": 0})], [finish()]]
    )
    audit = tmp_path / "teardown-audit.jsonl"
    server = {
        "transport": "stdio",
        "persistent_session": True,
        "command": sys.executable,
        "args": [
            str(Path(__file__).with_name("mcp_fixture_server.py")),
            "stdio",
            "0",
            str(audit),
        ],
    }
    entered, release, closed = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    tasks = []
    async with runtime(
        tmp_path, url, core_extra={"mcp": {"servers": {"counter": server}}}
    ) as (env, contexts, _):
        host = contexts.resource_host
        original_open = host.open

        async def close():
            tasks.append(asyncio.current_task())
            entered.set()
            await release.wait()
            await _close_mcp_owner()
            closed.set()

        async def open_owner(*args, **kwargs):
            return await original_open(*args, **kwargs, close=close)

        monkeypatch.setattr(host, "open", open_owner)
        handle = await env.client.start_workflow(
            NativeAgentWorkflow.run,
            request(),
            id="cancel-teardown",
            task_queue="native-controls",
        )
        try:
            await asyncio.wait_for(entered.wait(), 30)
            (owner,) = host._owners.values()
            assert tasks == [owner._task]
            await handle.cancel()
            # The activity cancellation must reach the waiter while the actor
            # still owns the live MCP scopes. It must not cancel that actor.
            await asyncio.sleep(0.2)
            assert not owner._task.done()
            release.set()
            await asyncio.wait_for(closed.wait(), 10)
            with pytest.raises(WorkflowFailureError) as cancelled:
                await asyncio.wait_for(handle.result(), 30)
            assert isinstance(cancelled.value.cause, CancelledError)
            assert (await handle.describe()).status.name == "CANCELED"
            assert owner._task.done()
            assert (
                host.ledger.get(owner.ref.owner_id, "__resource_owner__")[
                    "status"
                ]
                == "CLOSED"
            )
            assert len(tasks) == 1
            assert [
                json.loads(line)["event"]
                for line in audit.read_text().splitlines()
            ] == ["open", "increment"]
            assert len(requests) == 2
            assert not contexts._contexts and not host._owners
            assert (
                contexts.stores["files"]
                .read_snapshot("test", "controls")
                .owner_id
                is None
            )
            history = await handle.fetch_history()
        finally:
            release.set()
    await Replayer(
        workflows=[NativeAgentWorkflow],
        workflow_failure_exception_types=[Exception],
    ).replay_workflow(history)
    assert len(requests) == 2
