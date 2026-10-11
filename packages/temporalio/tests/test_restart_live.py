"""SIGKILL around actual native durable acknowledgements, then sandbox replay."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_core_temporalio.client import AgentClient
from gc_core_temporalio.contracts import RunRequest
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore
from gc_core_temporalio.thread_store_contracts import ThreadStoreCall
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from live_support import provider, temporal_cli, wait_marker  # noqa: F401
from restart_interceptor import RestartInterceptor
from temporalio.converter import default as default_converter
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
    binary = temporal_cli()
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
                    str(Path(__file__).with_name("worker_process.py")),
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
            client = AgentClient(env.client)
            handle = await client.start(
                RunRequest(
                    "test",
                    "selected",
                    "test",
                    "restart-thread",
                    [f"Read {tmp_path}/example.txt and record the result."],
                    "ua-resource-local",
                    "ua-model",
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
            history = await handle.fetch_history()
            if checkpoint == "after_store_receipt":
                # Воркер убит после записи, до ответа: повтор activity на
                # новом воркере (без кэша meta) собрал из разницы ту же
                # операцию, и хранилище ответило исходной квитанцией.
                marker = json.loads(
                    (tmp_path / "checkpoint.json").read_text()
                )["value"]
                original = value_to_type(ThreadStoreCall, marker["request"])
                assert original.meta_delta is not None
                assert original.operation.payload["meta"] is None
                assert marker["receipt"]["receipt"] in _receipts(history)
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
            workflows=[UniversalAgentWorkflow],
            interceptors=[RestartInterceptor()],
        ).replay_workflow(history)
        assert len(requests) == expected_requests


def _receipts(history):
    """Квитанции хранилища из завершённых activities истории."""
    converter = default_converter().payload_converter
    found = []
    for event in history.events:
        if not event.HasField("activity_task_completed_event_attributes"):
            continue
        payloads = event.activity_task_completed_event_attributes.result
        for value in converter.from_payloads(payloads.payloads):
            if isinstance(value, dict) and value.get("receipt"):
                found.append(value["receipt"])
    return found
