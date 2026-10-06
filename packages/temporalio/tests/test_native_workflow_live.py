"""Actual sandboxed native executor, HTTP model, FileThreadStore and replay."""

import asyncio
import json
import shutil
from collections import Counter

import pytest
from gc_core.run_context import acreate_run_context
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_ua_temporalio.native_activities import NativeActivities
from gc_ua_temporalio.native_contracts import NativeRunRequest
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from gc_ua_temporalio.public_config import public_config_snapshot
from gc_testkit.settings import make_settings
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from gc_ua_temporalio.thread_store_activities import ThreadStoreActivities
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from live_support import (
    provider,  # noqa: F401  # shared HTTP provider fixture
)


@pytest.mark.parametrize(
    ("batch_flags", "persist"),
    [(False, "step"), (True, "step"), (True, "turn")],
    ids=["legacy-flags", "batched-flags", "turn-persist"],
)
async def test_native_executor_uses_real_file_session_and_replays(
    tmp_path,
    provider,  # noqa: F811
    monkeypatch,
    batch_flags,
    persist,
):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
    monkeypatch.chdir(tmp_path)
    url, requests = provider
    (tmp_path / "example.txt").write_text("file from native workflow")
    config = make_settings(
        {
            "agent.model": "scripted",
            "backend.api_mode": "chat_completions",
            "backend.completions_url": url,
            "backend.chat_completion_kwargs": {"stream": False},
            "agent.system_prompt_id": "native-test",
            "agent.max_steps": 10,
            "launch_root": str(tmp_path),
            "logging.save_logs": False,
            "logging.console_trace": False,
            "agent.use_local_subagents": False,
            "agent.strict_turn_finish_validation": False,
            "tui.text_answer_mode": "deny",
            "thread_store.persist": persist,
        }
    )
    contexts = {}

    async def context_provider(call):
        if call.owner_id not in contexts:
            contexts[call.owner_id] = await acreate_run_context(
                config, run_id="native-live"
            )
        return contexts[call.owner_id]

    # История и мета агента — в переданном FileThreadStore.
    files = DurableFileThreadStore(str(tmp_path / "threads"))
    activities = NativeActivities(
        context_provider=context_provider,
        stores={"files": files},
        public_config=public_config_snapshot,
    )
    if not batch_flags:
        prepare = activities._prepare

        async def without_batch(call, ctx):
            result = await prepare(call, ctx)
            result.store_capabilities.remove("reset_run_flags")
            return result

        activities._prepare = without_batch
    thread = ThreadStoreActivities({"files": files})
    registered = [
        activities.prepare,
        activities.prepare_state,
        activities.prepare_prompts,
        activities.prompt,
        activities.skills,
        activities.model,
        activities.tool,
        activities.guard,
        activities.memory,
        activities.memory_hash,
        activities.summary_trace,
        activities.close,
        activities.release,
        thread.load,
        thread.commit,
        thread.read_trace,
    ]
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:
        async with Worker(
            env.client,
            task_queue="native-test",
            workflows=[NativeAgentWorkflow],
            activities=registered,
        ):
            handle = await env.client.start_workflow(
                NativeAgentWorkflow.run,
                NativeRunRequest(
                    "test",
                    "files",
                    "test",
                    "native-thread",
                    [f"Read {tmp_path}/example.txt and record the result."],
                    "native-test",
                    "native-test",
                ),
                id="native-test",
                task_queue="native-test",
            )
            try:
                result = await asyncio.wait_for(handle.result(), 45)
            except Exception as exc:
                cause = getattr(exc, "cause", None)
                print(getattr(cause, "failure", None))
                raise
            history = await handle.fetch_history()
        activity_counts = Counter(
            event.activity_task_scheduled_event_attributes.activity_type.name
            for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
        )
        failures = [
            str(event.activity_task_failed_event_attributes.failure)
            for event in history.events
            if event.HasField("activity_task_failed_event_attributes")
        ]
        assert not any(
            "ThreadStoreConflict" in failure for failure in failures
        )
        # Before own metadata acknowledgements: 13 commits and 4 loads, one
        # failed commit and its recovery load caused solely by our own flag.
        # Optional startup batching removes one metadata read and one commit.
        # persist: turn — шаги хода одним commit'ом в конце хода.
        assert (
            activity_counts["agent.thread.commit"]
            == ({"step": 11 if batch_flags else 12, "turn": 4}[persist])
        )
        assert activity_counts["agent.thread.load"] == (
            2 if batch_flags else 3
        )
        measurement = {
            "batch_flags": batch_flags,
            "persist": persist,
            "activities": dict(sorted(activity_counts.items())),
            "activity_total": sum(activity_counts.values()),
            "history_events": len(history.events),
        }
        print(
            "NATIVE_ACTIVITY_COUNTS " + json.dumps(measurement, sort_keys=True)
        )
        (tmp_path / "activity-counts.json").write_text(
            json.dumps(measurement, indent=2)
        )
        assert "Recovered successfully" in str(result["answer"])
        assert len(requests) == 2
        messages = files.load_messages("test", "native-thread")
        assert any(
            "file from native workflow" in str(message) for message in messages
        )
        assert (
            files.load_meta("test", "native-thread")["shared"][
                "working_memory"
            ]["knowledge"]
            == "durable fact"
        )
        await Replayer(workflows=[NativeAgentWorkflow]).replay_workflow(
            history
        )
        assert len(requests) == 2
