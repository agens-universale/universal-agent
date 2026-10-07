"""Worker Versioning: ход доигрывается своей сборкой, следующий — новой."""

import asyncio
import shutil
import time

import pytest
import test_workflow_controls as controls
from gc_core_temporalio.contracts import UserMessage
from gc_core_temporalio.provider import WorkerConfig
from gc_core_temporalio.worker import (
    build_runtime,
    set_current_version,
    worker_options,
)
from gc_core_temporalio.workflow import AgentWorkflow
from live_support import call
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_ownership_live import _config
from test_workflow_controls import finish, request, wait_status

scripted_http = controls.scripted_http


def _versioned(tmp_path, url, model, build):
    raw = _config(tmp_path, url, model).model_dump(mode="json")
    raw["versioning"] = {
        "enabled": True,
        "deployment_name": "ua-test",
        "build_id": build,
    }
    return WorkerConfig.model_validate(raw)


async def test_turn_is_pinned_and_next_turn_runs_on_new_build(
    tmp_path, monkeypatch, scripted_http
):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    (tmp_path / "a.txt").write_text("a")
    switched = asyncio.Event()

    def slow_tool_call():
        # Ход v1 ещё идёт, когда текущей объявят v2.
        deadline = time.time() + 20
        while not switched.is_set() and time.time() < deadline:
            time.sleep(0.1)
        return [call("r", "fs_read_file", {"path": "a.txt"})]

    script.extend([slow_tool_call, [finish("first")], [finish("second")]])
    v1 = _versioned(tmp_path, url, "model-a", "v1")
    v2 = _versioned(tmp_path, url, "model-b", "v2")
    runtimes = []
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:

            def worker(config):
                contexts, native, threads = build_runtime(config)
                runtimes.append(contexts)
                (options,) = worker_options(config, native, threads)
                return Worker(env.client, **options)

            async with worker(v1):
                await set_current_version(env.client, v1)
                handle = await env.client.start_workflow(
                    AgentWorkflow.run,
                    request(keep_alive=True),
                    id="controls",
                    task_queue="native-controls",
                )
                await wait_status(
                    handle, lambda s: s["phase"] == "running" and requests
                )
                async with worker(v2):
                    await set_current_version(env.client, v2)
                    switched.set()
                    await wait_status(
                        handle,
                        lambda s: s["turns"] == 1 and s["phase"] == "idle",
                    )
                    # Весь первый ход — на v1, хотя текущей уже стала v2.
                    assert {body["model"] for body in requests} == {"model-a"}
                    await handle.execute_update(
                        AgentWorkflow.submit_message,
                        UserMessage("second", "second task"),
                    )
                    await wait_status(
                        handle,
                        lambda s: s["turns"] == 2 and s["phase"] == "idle",
                    )
                    assert requests[-1]["model"] == "model-b"
                    await handle.signal(AgentWorkflow.close_session)
                    await asyncio.wait_for(handle.result(), 30)
    finally:
        for contexts in runtimes:
            await contexts.aclose()
