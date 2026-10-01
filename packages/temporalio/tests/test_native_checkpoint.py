"""Immutable checkpoint integrity and real server payload-bounded CAN."""

import asyncio
import base64
import hashlib
import shutil
from dataclasses import replace

import pytest
from gc_ua_temporalio.native_checkpoint import (
    CHUNK_BYTES,
    NativeCheckpointActivities,
    NativeCheckpointStore,
)
from gc_ua_temporalio.native_checkpoint_contracts import (
    CheckpointNamespace,
    NativeCheckpointChunk,
    NativeCheckpointRef,
)
from gc_ua_temporalio.native_contracts import NativeRunRequest
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from native_checkpoint_workflow import (
    LargeCheckpointWorkflow,
    SmallCheckpointNativeWorkflow,
)
from temporalio import activity
from temporalio.client import WorkflowUpdateStage
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_live_workflow import call
from test_native_workflow_controls import scripted_http  # noqa: F401


def chunks(raw):
    ref = NativeCheckpointRef(
        CheckpointNamespace("workflow", "p", "v1", "files", "s", "t"),
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES,
    )
    return ref, [
        NativeCheckpointChunk(
            ref,
            i,
            base64.b64encode(
                raw[i * CHUNK_BYTES : (i + 1) * CHUNK_BYTES]
            ).decode(),
        )
        for i in range(ref.chunk_count)
    ]


def test_immutable_checkpoint_original_retry_integrity_and_namespace(tmp_path):
    store = NativeCheckpointStore(
        SQLiteThreadStore(tmp_path / "receipts.sqlite")
    )
    ref, parts = chunks(b"x" * (CHUNK_BYTES + 3))
    store.put(parts[0])
    with pytest.raises(ValueError, match="Incomplete"):
        store.seal(ref)
    for part in parts:
        assert store.put(part) and store.put(part)
    assert store.seal(ref) == store.seal(ref) == ref
    assert [store.get(part) for part in parts] == parts
    with pytest.raises(ValueError, match="manifest conflict"):
        store.put(
            NativeCheckpointChunk(
                replace(
                    ref,
                    byte_length=ref.byte_length + CHUNK_BYTES,
                    chunk_count=ref.chunk_count + 1,
                ),
                ref.chunk_count,
                base64.b64encode(b"bad").decode(),
            )
        )
    with pytest.raises(ValueError, match="Immutable"):
        store.put(replace(parts[-1], data=base64.b64encode(b"bad").decode()))
    with pytest.raises(ValueError, match="Missing"):
        store.get(
            replace(
                parts[0],
                ref=replace(
                    ref, namespace=replace(ref.namespace, revision="other")
                ),
            )
        )
    with store.receipts.transaction() as db:
        db.execute(
            "UPDATE native_delivery_chunks SET data=? WHERE idx=1", (b"bad",)
        )
    with pytest.raises(ValueError, match="corrupt"):
        store.seal(ref)


async def test_checkpoint_over_server_payload_limit_can_and_replay(tmp_path):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
    checkpoints = NativeCheckpointActivities(
        SQLiteThreadStore(tmp_path / "receipts.sqlite")
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:
        async with Worker(
            env.client,
            task_queue="checkpoint",
            workflows=[LargeCheckpointWorkflow],
            activities=[checkpoints.put, checkpoints.seal, checkpoints.get],
        ):
            handle = await env.client.start_workflow(
                LargeCheckpointWorkflow.run,
                NativeRunRequest(
                    "p",
                    "v1",
                    "files",
                    "s",
                    "t",
                    [],
                    "checkpoint",
                    "checkpoint",
                ),
                id="large-checkpoint",
                task_queue="checkpoint",
            )
            result = await asyncio.wait_for(handle.result(), 45)
            assert result["size"] > 2 * 1024 * 1024
            assert result["replies"] == 12 and result["accepted"] == 500
            first = await env.client.get_workflow_handle(
                "large-checkpoint", run_id=handle.first_execution_run_id
            ).fetch_history()
            latest = await handle.fetch_history()
        for history in (first, latest):
            for event in history.events:
                if event.HasField("activity_task_scheduled_event_attributes"):
                    payload = (
                        event.activity_task_scheduled_event_attributes.input
                    )
                    assert payload.ByteSize() < 128 * 1024
                if event.HasField("activity_task_completed_event_attributes"):
                    assert (
                        event.activity_task_completed_event_attributes.result.ByteSize()
                        < 128 * 1024
                    )
                if event.HasField(
                    "workflow_execution_continued_as_new_event_attributes"
                ):
                    assert (
                        event.workflow_execution_continued_as_new_event_attributes.input.ByteSize()
                        < 4096
                    )
            await Replayer(
                workflows=[LargeCheckpointWorkflow]
            ).replay_workflow(history)


@pytest.mark.parametrize("gate", ["upload", "restore"])
async def test_real_native_checkpoint_delivery_races(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
    gate,
):
    import gc_ua_temporalio.native_worker as worker_module
    import test_native_workflow_controls as controls
    from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
    from gc_ua_temporalio.session_contracts import UserMessage

    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [
            [call("ask", "turn_ask_human", {"question": "Choose"})],
            [controls.finish("first")],
            [controls.finish("second")],
        ]
    )
    entered, release = asyncio.Event(), asyncio.Event()
    blocked = False

    class GatedCheckpoints(NativeCheckpointActivities):
        async def gate_once(self, selected):
            nonlocal blocked
            if gate == selected and not blocked:
                blocked = True
                entered.set()
                await release.wait()

        @activity.defn(name="agent.delivery.put")
        async def put(self, chunk: NativeCheckpointChunk) -> bool:
            await self.gate_once("upload")
            return await super().put(chunk)

        @activity.defn(name="agent.delivery.get")
        async def get(
            self, chunk: NativeCheckpointChunk
        ) -> NativeCheckpointChunk:
            await self.gate_once("restore")
            return await super().get(chunk)

    original_options = controls.worker_options

    def options(*args):
        entries = original_options(*args)
        for entry in entries:
            entry["workflows"] = [
                SmallCheckpointNativeWorkflow
                if item is NativeAgentWorkflow
                else item
                for item in entry["workflows"]
            ]
        return entries

    monkeypatch.setattr(
        worker_module, "NativeCheckpointActivities", GatedCheckpoints
    )
    monkeypatch.setattr(controls, "worker_options", options)
    async with controls.runtime(tmp_path, url) as (env, contexts, _):
        handle = await env.client.start_workflow(
            SmallCheckpointNativeWorkflow.run,
            controls.request(
                keep_alive=True,
                continue_after_turns=1,
                message_ids=["initial"],
            ),
            id="checkpoint-race",
            task_queue="native-controls",
        )
        first_run = handle.first_execution_run_id
        status = await controls.wait_status(
            handle, lambda value: value["phase"] == "waiting_human"
        )
        request_id = status["pending_human"]["request_id"]
        await handle.execute_update(
            NativeAgentWorkflow.answer_human,
            args=[request_id, "blue", "reply-1"],
        )
        try:
            await asyncio.wait_for(entered.wait(), 30)
            if gate == "upload":
                receipt = await handle.execute_update(
                    NativeAgentWorkflow.submit_message,
                    UserMessage("second", "second task"),
                )
                assert not receipt["duplicate"]
            else:
                duplicate_reply = await handle.start_update(
                    NativeAgentWorkflow.answer_human,
                    args=[request_id, "blue", "reply-1"],
                    wait_for_stage=WorkflowUpdateStage.ACCEPTED,
                )
                duplicate_message = await handle.start_update(
                    NativeAgentWorkflow.submit_message,
                    UserMessage("initial", "first task"),
                    wait_for_stage=WorkflowUpdateStage.ACCEPTED,
                )
                await handle.signal(NativeAgentWorkflow.stop)
            await handle.signal(NativeAgentWorkflow.close_session)
            release.set()
            if gate == "restore":
                assert await duplicate_reply.result()
                assert (await duplicate_message.result())["duplicate"]
            result = await asyncio.wait_for(handle.result(), 30)
            assert result["turns"] == (2 if gate == "upload" else 1)
            assert len(requests) == (3 if gate == "upload" else 2)
            if gate == "upload":
                messages = contexts.stores["files"].load_messages(
                    "test", "controls"
                )
                assert (
                    sum(
                        message.get("content") == "second task"
                        for message in messages
                    )
                    == 1
                )
            else:
                assert (await handle.query(NativeAgentWorkflow.status))[
                    "stop_requested"
                ]
            first = await env.client.get_workflow_handle(
                "checkpoint-race", run_id=first_run
            ).fetch_history()
            latest = await handle.fetch_history()
        finally:
            release.set()
        for history in (first, latest):
            await Replayer(
                workflows=[SmallCheckpointNativeWorkflow]
            ).replay_workflow(history)
