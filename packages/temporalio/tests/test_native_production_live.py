"""Exercise the standalone native wiring against actual Temporal and HTTP."""

import asyncio
import shutil

import pytest
from gc_ua_temporalio.native_client import NativeAgentClient
from gc_ua_temporalio.native_contracts import NativeRunRequest
from gc_ua_temporalio.native_provider import NativeWorkerConfig
from gc_ua_temporalio.native_worker import build_native_runtime, worker_options
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from live_support import provider  # noqa: F401


@pytest.mark.parametrize("provider_unavailable", [False, True])
async def test_production_provider_worker_client_and_replay(
    tmp_path,
    monkeypatch,
    provider,  # noqa: F811
    provider_unavailable,
):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
    monkeypatch.chdir(tmp_path)
    url, requests = provider
    (tmp_path / "example.txt").write_text(
        "production provider file", encoding="utf-8"
    )
    config = NativeWorkerConfig.model_validate(
        {
            "workflow_queue": "native-production",
            "model_queue": "native-production",
            "resource_queue": "native-production",
            "receipts_database": str(tmp_path / "receipts.sqlite"),
            "profiles": {
                "test": {
                    "revision": "v1",
                    "workspace": str(tmp_path),
                    "core": {
                        "backend": {
                            "api_mode": "chat_completions",
                            "completions_url": "http://127.0.0.1:1"
                            if provider_unavailable
                            else url,
                            "chat_completion_kwargs": {"stream": False},
                        },
                        "agent": {
                            "model": "scripted",
                            "system_prompt_id": "native-test",
                            "max_steps": 10,
                            "strict_turn_finish_validation": False,
                        },
                        "tui": {"text_answer_mode": "deny"},
                        "logging": {
                            "save_logs": False,
                            "console_trace": False,
                        },
                    },
                }
            },
        }
    )
    contexts, native, threads = build_native_runtime(config)
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            (options,) = worker_options(config, native, threads)
            async with Worker(
                env.client,
                **options,
            ):
                client = NativeAgentClient(
                    env.client, workflow_queue=config.workflow_queue
                )
                handle = await client.start(
                    NativeRunRequest(
                        "test",
                        "v1",
                        "files",
                        "test",
                        "production-thread",
                        [
                            f"Read {tmp_path}/example.txt and record the result."
                        ],
                        config.resource_queue,
                        config.model_queue,
                    ),
                    workflow_id="native-production-test",
                )
                if provider_unavailable:
                    with pytest.raises(WorkflowFailureError) as failure:
                        await asyncio.wait_for(handle.result(), 20)
                    assert failure.value.cause.type == "ModelProviderError"
                    history = await handle.fetch_history()
                    await Replayer(
                        workflows=[NativeAgentWorkflow]
                    ).replay_workflow(history)
                    assert not requests
                    assert contexts.stores["files"].load_messages(
                        "test", "production-thread"
                    )
                    return
                result = await asyncio.wait_for(handle.result(), 45)
                history = await handle.fetch_history()
            assert "Recovered successfully" in str(result["answer"])
            assert len(requests) == 2
            files = contexts.stores["files"]
            assert any(
                "production provider file" in str(message)
                for message in files.load_messages("test", "production-thread")
            )
            assert (
                files.load_meta("test", "production-thread")["shared"][
                    "working_memory"
                ]["knowledge"]
                == "durable fact"
            )
            await Replayer(workflows=[NativeAgentWorkflow]).replay_workflow(
                history
            )
            assert len(requests) == 2
    finally:
        await contexts.aclose()
