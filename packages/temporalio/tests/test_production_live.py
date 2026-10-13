"""Exercise the standalone native wiring against actual Temporal and HTTP."""

import asyncio
import shutil

import pytest
from gc_core_temporalio.client import AgentClient
from gc_core_temporalio.contracts import RunRequest
from gc_core_temporalio.provider import WorkerConfig
from gc_core_temporalio.worker import build_runtime
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from live_support import (  # noqa: F401
    provider,
    temporal_cli,
    worker_kwargs,
)


@pytest.mark.parametrize("provider_unavailable", [False, True])
@pytest.mark.parametrize("old_routing_history", [False, True])
async def test_production_provider_worker_client_and_replay(
    tmp_path,
    monkeypatch,
    provider,  # noqa: F811
    provider_unavailable,
    old_routing_history,
):
    binary = temporal_cli()
    monkeypatch.chdir(tmp_path)
    url, requests = provider
    (tmp_path / "example.txt").write_text(
        "production provider file", encoding="utf-8"
    )
    config = WorkerConfig.model_validate(
        {
            "workflow_queue": "native-production",
            "model_queue": "native-production",
            "resource_queue": "native-production",
            "state_dir": str(tmp_path),
            # По умолчанию недоступный провайдер Temporal повторяет без
            # ограничения (ход ждёт). Здесь лимит задан явно: после него —
            # отказ сессии (FAILED).
            "activities": {
                "model": {
                    "retry": {"maximum_attempts": 2, "initial_interval": 0.1}
                }
            },
            "profiles": {
                "test": {
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
    original_patched = workflow.patched
    if old_routing_history:
        monkeypatch.setattr(workflow, "patched", lambda marker: (
            False if marker == "routing-request-contract-v2" else original_patched(marker)
        ))
    contexts, native, threads = build_runtime(config)
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            options = worker_kwargs(config, native, threads)
            async with Worker(
                env.client,
                **options,
            ):
                client = AgentClient(
                    env.client, workflow_queue=config.workflow_queue
                )
                handle = await client.start(
                    RunRequest(
                        "test",
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
                    assert failure.value.cause.type == "InfrastructureFailure"
                    assert "Model provider unavailable" in str(
                        failure.value.cause
                    )
                    history = await handle.fetch_history()
                    monkeypatch.setattr(workflow, "patched", original_patched)
                    await Replayer(
                        workflows=[UniversalAgentWorkflow]
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
            finals = [r["content"] for r in files.load_meta("test", "production-thread")["user_intent_log"]
                      if r["action"] == "final_answer"]
            if old_routing_history:
                assert finals == []
            else:
                assert any("Recovered successfully" in text for text in finals)
            # Replay with the current marker implementation against both histories.
            monkeypatch.setattr(workflow, "patched", original_patched)
            await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(
                history
            )
            assert len(requests) == 2
    finally:
        await contexts.aclose()
