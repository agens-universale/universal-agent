"""SIGKILL after actual HTTP result: operator receipt resumes the same workflow."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict

import pytest
from gc_core.config_modules import AppConfig
from gc_core.execution.contracts import ExecutionFailure
from gc_ua_temporalio.native_client import NativeAgentClient
from gc_ua_temporalio.native_contracts import NativeRunRequest
from gc_ua_temporalio.native_operator_contracts import (
    NativeOperatorResolution,
    result_digest,
)
from gc_ua_temporalio.native_provider import (
    NativeProfile,
    NativeStoreConfig,
    NativeWorkerConfig,
)
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from provider_fixture import calls, finish, scripted_provider
from temporalio.client import WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

WORKER = """
import asyncio, json, os
from dataclasses import asdict
from pathlib import Path
from gc_ua_temporalio.native_activities import NativeActivities
from gc_ua_temporalio.native_provider import NativeWorkerConfig
from gc_ua_temporalio.native_worker import serve
if os.environ.get("CAPTURE_MODEL"):
    original = NativeActivities._model
    async def capture(self, call, ctx):
        result = await original(self, call, ctx)
        if os.environ.get("CAPTURE_CHILD") and call.child_context is None:
            return result
        Path(os.environ["CAPTURE_MODEL"]).write_text(json.dumps(asdict(result)))
        await asyncio.Event().wait()
    NativeActivities._model = capture
asyncio.run(serve(NativeWorkerConfig.load(os.environ["NATIVE_CONFIG"])))
"""


async def until(callback, timeout=65):
    async with asyncio.timeout(timeout):
        while not (value := await callback()):
            await asyncio.sleep(0.1)
        return value


async def settle_closed(config, client, captured):
    from gc_ua_temporalio.native_closed_recovery import NativeClosedRecovery
    from gc_ua_temporalio.native_operator_contracts import (
        NativeOperatorCall,
        NativePendingOperation,
    )
    from temporalio.converter import value_to_type

    recovery = NativeClosedRecovery(config, client, "files")
    inspection = await recovery.inspect("test", "thread")
    effect = inspection["unresolved"][0]
    with pytest.raises(ValueError, match=effect["operation_id"]):
        await recovery.release(
            "test", "thread", "release-1", evidence="Verified terminal run"
        )
    pending = value_to_type(NativePendingOperation, effect["pending"])
    result = json.loads(captured.read_text())
    from gc_ua_temporalio.native_operator_contracts import operation_digest

    resolution = NativeOperatorResolution(
        "closed-receipt",
        pending.call.operation_id,
        operation_digest(pending.call),
        effect["ledger_digest"],
        effect["status"],
        {
            "source": str(captured),
            "note": "Verified canonical response before SIGKILL",
            "result_sha256": result_digest(result),
        },
        result,
    )
    assert (
        await recovery.resolve(
            "test", "thread", NativeOperatorCall(pending, resolution)
        )
    )["resolved"]
    assert (
        await recovery.release(
            "test", "thread", "release-1", evidence="Verified terminal run"
        )
    )["released"]
    assert recovery.store.read_snapshot("test", "thread").owner_id is None


@pytest.mark.parametrize(
    "abort,child",
    [(None, False), ("stop", False), ("close", False), (None, True)],
    ids=["root-resolve", "root-stop", "root-close", "child"],
)
async def test_sigkill_known_model_receipt_resumes_without_second_http(
    tmp_path,
    abort,
    child,
):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = tmp_path / "known-receipt.json"
    config_path = tmp_path / "worker.json"
    processes = []
    logs = []

    def respond(data):
        is_child = any(
            "name: subagent_worker" in str(message.get("content"))
            for message in data["messages"]
            if message["role"] == "system"
        )
        if (
            child
            and not is_child
            and not any(
                message["role"] == "tool" for message in data["messages"]
            )
        ):
            return calls(
                (
                    "delegate",
                    "subagent_worker",
                    {"task": "Finish the task", "reason": "Delegate"},
                )
            )
        return finish("known result")

    with scripted_provider(respond) as (url, requests):
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            config = NativeWorkerConfig(
                address=env.client.service_client.config.target_host,
                workflow_queue="operator-test",
                model_queue="operator-test",
                resource_queue="operator-test",
                receipts_database=str(tmp_path / "receipts.sqlite"),
                thread_stores={
                    "files": NativeStoreConfig(path=str(tmp_path / "threads"))
                },
                profiles={
                    "test": NativeProfile(
                        revision="v1",
                        workspace=str(workspace),
                        core=AppConfig.model_validate(
                            {
                                "backend": {
                                    "api_mode": "chat_completions",
                                    "completions_url": url,
                                    "stream": False,
                                    "chat_completion_kwargs": {
                                        "stream": False
                                    },
                                },
                                "agent": {
                                    "model": "scripted",
                                    "max_steps": 3,
                                    "strict_turn_finish_validation": False,
                                    "use_local_subagents": child,
                                },
                                "logging": {
                                    "save_logs": False,
                                    "console_trace": False,
                                },
                                "tui": {"text_answer_mode": "deny"},
                            }
                        ),
                    )
                },
            )
            config_path.write_text(config.model_dump_json())

            def spawn(capture=False):
                log = open(tmp_path / f"worker-{len(processes)}.log", "w")  # noqa: SIM115 -- closed in outer finally
                logs.append(log)
                variables = {**os.environ, "NATIVE_CONFIG": str(config_path)}
                if capture:
                    variables["CAPTURE_MODEL"] = str(captured)
                    if child:
                        variables["CAPTURE_CHILD"] = "1"
                process = subprocess.Popen(
                    [sys.executable, "-c", WORKER],
                    env=variables,
                    cwd=workspace,
                    stdout=log,
                    stderr=log,
                )
                processes.append(process)
                return process

            first = spawn(True)
            client = NativeAgentClient(
                env.client, workflow_queue="operator-test"
            )
            try:
                handle = await client.start(
                    NativeRunRequest(
                        "test",
                        "v1",
                        "files",
                        "test",
                        "thread",
                        ["Finish"],
                        "operator-test",
                        "operator-test",
                        keep_alive=True,
                    ),
                    workflow_id="operator-test",
                )

                async def has_receipt():
                    return captured.exists()

                await until(has_receipt)
                first.kill()
                await asyncio.to_thread(first.wait, 10)
                spawn()

                async def pending():
                    status = await client.status(handle.id)
                    return status if status.get("pending_operation") else None

                status = await until(pending)
                child_id = status.get("active_child")
                if child:
                    assert (
                        child_id
                        and status["pending_operation"]["owner_id"] == child_id
                    )
                assert status["phase"] == "needs_reconciliation"
                if abort:
                    if abort == "stop":
                        await client.stop_turn(handle.id, status["turn_id"])
                    else:
                        await client.close_session(handle.id)
                    with pytest.raises(WorkflowFailureError):
                        await asyncio.wait_for(handle.result(), 30)
                    assert len(requests) == (3 if child else 1)
                    await Replayer(
                        workflows=[NativeAgentWorkflow],
                        workflow_failure_exception_types=[ExecutionFailure],
                    ).replay_workflow(await handle.fetch_history())
                    assert len(requests) == (3 if child else 1)
                    await settle_closed(config, env.client, captured)
                    return
                inspected = await client.inspect_operation(handle.id)
                result = json.loads(captured.read_text())
                resolution = NativeOperatorResolution(
                    "verified-1",
                    inspected["operation_id"],
                    inspected["digest"],
                    inspected["ledger_digest"],
                    inspected["status"],
                    {
                        "source": str(captured),
                        "note": "Captured canonical model response before worker SIGKILL",
                        "result_sha256": result_digest(result),
                    },
                    result,
                )
                bad = asdict(resolution)
                bad["digest"] = "stale"
                with pytest.raises(WorkflowUpdateFailedError):
                    await client.resolve_operation(
                        handle.id, NativeOperatorResolution(**bad)
                    )
                assert (await client.resolve_operation(handle.id, resolution))[
                    "resolved"
                ]

                async def idle():
                    return (await client.status(handle.id))["phase"] == "idle"

                await until(idle, timeout=30)
                assert (await client.resolve_operation(handle.id, resolution))[
                    "duplicate"
                ]
                await client.close_session(handle.id)
                outcome = await asyncio.wait_for(handle.result(), 30)
                assert "known result" in str(outcome["answer"])
                assert len(requests) == (3 if child else 1)
                history = await handle.fetch_history()
                await Replayer(
                    workflows=[NativeAgentWorkflow]
                ).replay_workflow(history)
                if child:
                    from gc_ua_temporalio.native_child_workflow import (
                        NativeSubagentWorkflow,
                    )

                    child_history = await env.client.get_workflow_handle(
                        child_id
                    ).fetch_history()
                    await Replayer(
                        workflows=[NativeSubagentWorkflow]
                    ).replay_workflow(child_history)
                assert len(requests) == (3 if child else 1)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    await asyncio.to_thread(process.wait, 10)
                for log in logs:
                    log.close()
