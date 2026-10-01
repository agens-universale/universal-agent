"""Test workflow hosts for bounded checkpoint transport and real native CAN races."""

import hashlib
from dataclasses import replace

from gc_ua_temporalio.native_checkpoint_transport import (
    encode_resume,
    load_checkpoint,
    save_checkpoint,
)
from gc_ua_temporalio.native_contracts import NativeResume, NativeRunRequest
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from temporalio import workflow


@workflow.defn
class LargeCheckpointWorkflow:
    @workflow.run
    async def run(self, start: NativeRunRequest) -> dict:
        if start.resume_ref is None:
            resume = NativeResume(
                human_replies={
                    f"request-{i}": "Я" * 100_000 for i in range(12)
                },
                accepted={f"message-{i}": "a" * 64 for i in range(500)},
            )
            raw = encode_resume(resume)
            ref = await save_checkpoint(start, raw)
            workflow.continue_as_new(replace(start, resume_ref=ref))
        restored = await load_checkpoint(start)
        raw = encode_resume(restored)
        return {
            "digest": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "replies": len(restored.human_replies),
            "accepted": len(restored.accepted),
        }


@workflow.defn
class SmallCheckpointNativeWorkflow(NativeAgentWorkflow):
    @workflow.run
    async def run(self, start: NativeRunRequest) -> dict:
        return await super().run(start)

    async def _continue(self, start, store):
        import gc_ua_temporalio.native_workflow as native_module

        native_module.INLINE_BYTES = 1
        await super()._continue(start, store)
