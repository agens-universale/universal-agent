"""Sandbox-safe bounded payload transport for large Continue-As-New delivery state."""

import base64
import hashlib
import json
from dataclasses import asdict
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.converter import value_to_type

from gc_ua_temporalio.native_checkpoint_contracts import (
    CheckpointNamespace,
    NativeCheckpointChunk,
    NativeCheckpointRef,
)

CHUNK_BYTES = 64 * 1024
INLINE_BYTES = 128 * 1024


def checkpoint_namespace(start):
    return CheckpointNamespace(
        workflow.info().workflow_id,
        start.profile,
        start.revision,
        start.store_id,
        start.system_id,
        start.thread_id,
    )


def encode_resume(resume):
    return json.dumps(
        asdict(resume),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


async def _call(start, name, value, result_type):
    return await workflow.execute_activity(
        name,
        value,
        result_type=result_type,
        task_queue=start.resource_queue,
        start_to_close_timeout=timedelta(minutes=1),
        retry_policy=RetryPolicy(maximum_attempts=5),
        summary="Save delivery checkpoint"
        if name != "agent.delivery.get"
        else "Restore delivery checkpoint",
    )


async def save_checkpoint(start, raw):
    ref = NativeCheckpointRef(
        checkpoint_namespace(start),
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES,
    )
    for index in range(ref.chunk_count):
        data = base64.b64encode(
            raw[index * CHUNK_BYTES : (index + 1) * CHUNK_BYTES]
        ).decode("ascii")
        await _call(
            start,
            "agent.delivery.put",
            NativeCheckpointChunk(ref, index, data),
            bool,
        )
    return await _call(start, "agent.delivery.seal", ref, NativeCheckpointRef)


async def load_checkpoint(start):
    from gc_ua_temporalio.native_contracts import NativeResume

    ref = start.resume_ref
    if (
        ref.schema_version != 1
        or ref.namespace != checkpoint_namespace(start)
        or ref.chunk_count
        != (ref.byte_length + CHUNK_BYTES - 1) // CHUNK_BYTES
        or ref.byte_length < 1
    ):
        raise ValueError(
            "Delivery checkpoint belongs to another execution namespace"
        )
    chunks = []
    for index in range(ref.chunk_count):
        chunk = await _call(
            start,
            "agent.delivery.get",
            NativeCheckpointChunk(ref, index),
            NativeCheckpointChunk,
        )
        if chunk.ref != ref or chunk.index != index:
            raise ValueError("Delivery checkpoint chunk identity mismatch")
        chunks.append(base64.b64decode(chunk.data, validate=True))
    raw = b"".join(chunks)
    if (
        len(raw) != ref.byte_length
        or hashlib.sha256(raw).hexdigest() != ref.digest
    ):
        raise ValueError("Delivery checkpoint integrity check failed")
    return value_to_type(NativeResume, json.loads(raw))
