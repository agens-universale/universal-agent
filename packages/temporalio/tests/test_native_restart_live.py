"""SIGKILL around actual native durable acknowledgements, then sandbox replay."""

import asyncio
import json
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_ua_temporalio.native_client import NativeAgentClient
from gc_ua_temporalio.native_contracts import NativeRunRequest
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from gc_ua_temporalio.thread_store_contracts import ThreadStoreCall
from live_support import provider, wait_marker  # noqa: F401
from native_restart_interceptor import RestartInterceptor
from temporalio.converter import value_to_type
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer


@pytest.mark.parametrize("backend", ["file", "sqlite"])
@pytest.mark.parametrize(
    ("checkpoint", "persist"),
    [
        ("before_store_commit", "step"),
        ("after_store_receipt", "step"),
        ("after_model_activity", "step"),
        ("before_model_activity", "step"),
        ("before_model_request", "step"),
        ("after_model_response", "step"),
        # persist: turn — первая запись треда в конце хода, после модели.
        ("before_store_commit", "turn"),
        ("after_store_receipt", "turn"),
    ],
)
async def test_native_sigkill_durable_boundaries(
    tmp_path,
    provider,  # noqa: F811
    backend,
    checkpoint,
    persist,
):
    binary = shutil.which("temporal")
    if not binary:
        pytest.skip("Temporal CLI is required")
    url, requests = provider
    (tmp_path / "example.txt").write_text("native durable restart")
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:
        store_path = tmp_path / (
            "threads" if backend == "file" else "threads.sqlite"
        )
        config = {
            "address": env.client.service_client.config.target_host,
            "state_dir": str(tmp_path),
            "thread_stores": {
                "selected": {"kind": backend, "path": str(store_path)}
            },
            "profiles": {
                "test": {
                    "workspace": str(tmp_path),
                    "store_id": "selected",
                    "core": {
                        "backend": {
                            "api_mode": "chat_completions",
                            "completions_url": url,
                            "chat_completion_kwargs": {"stream": False},
                        },
                        "agent": {
                            "model": "scripted",
                            "system_prompt_id": "native-test",
                            "max_steps": 10,
                            "strict_turn_finish_validation": False,
                        },
                        "tui": {"text_answer_mode": "deny"},
                        "thread_store": {"persist": persist},
                        "logging": {
                            "save_logs": False,
                            "console_trace": False,
                        },
                    },
                }
            },
        }
        config_path = tmp_path / "worker.json"
        config_path.write_text(json.dumps(config))
        processes, logs = [], []

        def start(kind):
            log = (tmp_path / f"worker-{len(processes)}.log").open("w")
            logs.append(log)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("native_worker_process.py")),
                    str(config_path),
                    kind,
                    str(tmp_path / "checkpoint.json"),
                ],
                cwd=tmp_path,
                stdout=log,
                stderr=log,
            )
            processes.append(process)
            return process

        try:
            first = start(checkpoint)
            client = NativeAgentClient(env.client)
            handle = await client.start(
                NativeRunRequest(
                    "test",
                    "selected",
                    "test",
                    "restart-thread",
                    [f"Read {tmp_path}/example.txt and record the result."],
                    "ua-native-resource-local",
                    "ua-native-model",
                ),
                workflow_id="native-restart",
            )
            await wait_marker(tmp_path / "checkpoint.json", first, handle)
            first.kill()
            first.wait(timeout=10)
            count_at_crash = len(requests)
            assert count_at_crash == (
                2
                if persist == "turn"
                else 1
                if checkpoint
                in {"after_model_activity", "after_model_response"}
                else 0
            )
            start("none")
            # Как в pydantic-ai: запрос к модели повторяется после падения
            # («хотя бы раз»). Повторная оплата — если ответ уже пришёл, а
            # activity не успела завершиться.
            expected_requests = (
                3
                if checkpoint
                in {"after_model_activity", "after_model_response"}
                else 2
            )
            result = await asyncio.wait_for(handle.result(), 90)
            assert "Recovered successfully" in str(result["answer"])
            assert len(requests) == expected_requests
            store = (
                DurableFileThreadStore(str(store_path))
                if backend == "file"
                else SQLiteThreadStore(str(store_path))
            )
            messages = store.load_messages("test", "restart-thread")
            assert len({message["id"] for message in messages}) == len(
                messages
            )
            assert any(
                "native durable restart" in str(message)
                for message in messages
            )
            assert (
                store.load_meta("test", "restart-thread")["shared"][
                    "working_memory"
                ]["knowledge"]
                == "durable fact"
            )
            if checkpoint == "after_store_receipt":
                marker = json.loads(
                    (tmp_path / "checkpoint.json").read_text()
                )["value"]
                original = value_to_type(ThreadStoreCall, marker["request"])
                retried = store.commit_operation(
                    original.system_id,
                    original.thread_id,
                    original.operation,
                )
                assert asdict(retried) == marker["receipt"]["receipt"]
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
        ).replay_workflow(history)
        assert len(requests) == expected_requests
