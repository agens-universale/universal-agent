"""Control-aware adapter; the original core loop still owns tool batching."""

import asyncio
import json
from dataclasses import replace

from gc_core.execution.contracts import (
    ExecutionFailure,
    ModelResult,
    ToolResult,
)
from gc_core.execution.reducers import apply_model_result
from gc_core.execution.tool_policy import GUARDED_TOOLS

from gc_ua_temporalio.child_operations import invoke_child
from gc_ua_temporalio.contracts import ToolActivityInput
from gc_ua_temporalio.control import SessionControl
from gc_ua_temporalio.control_contracts import (
    EffectResult,
    ExecuteEffectInput,
    HumanDecision,
    JudgeDecision,
    PreparedTool,
    PrepareToolInput,
)
from gc_ua_temporalio.mcp_operations import invoke_mcp
from gc_ua_temporalio.operations import TemporalOperations, execute


def stopped_result():
    return ToolResult(
        json.dumps(
            {
                "ok": False,
                "error": {
                    "type": "user_stop",
                    "message": "Tool interrupted by user stop",
                },
            }
        ),
        outcome="tool_error",
    )


class ControlledOperations(TemporalOperations):
    def __init__(self, start, owner, control: SessionControl):
        super().__init__(start, owner)
        self.control = control

    async def model(self, request):
        if self.control.stopped:
            return ModelResult()
        try:
            return await self.control.run(super().model(request))
        except (asyncio.CancelledError, ExecutionFailure):
            if not self.control.stopped:
                raise
            return ModelResult()

    async def tool(self, invocation):
        if self.control.stopped:
            return stopped_result()
        engine = self.control.session.engine
        metadata = (
            engine.snapshot.tool_metadata.get(invocation.name, {})
            if engine
            else {}
        )
        if "mcp" in metadata:
            return await invoke_mcp(self, invocation, metadata)
        if "child_profile" in metadata:
            return await invoke_child(self, invocation)
        if invocation.name == "session_compact":
            return await self.control.session.engine.summary.compact(
                "tool", tool=invocation
            )
        if invocation.name in GUARDED_TOOLS:
            return await self._guarded(invocation)
        try:
            return await self.control.run(super().tool(invocation))
        except (asyncio.CancelledError, ExecutionFailure):
            if not self.control.stopped:
                raise
            return stopped_result()

    async def _guarded(self, invocation):
        data = PrepareToolInput(
            ToolActivityInput(
                self.start.profile,
                self.start.profile_revision,
                self.owner,
                invocation,
            )
        )
        for sequence in range(1, 17):
            data.sequence = sequence
            if self.control.stopped:
                return stopped_result()
            plan = await self._prepare(data)
            if plan.judge is not None:
                result = await self.model(
                    replace(
                        plan.judge,
                        operation_id=f"{invocation.operation_id}:judge:{sequence}",
                    )
                )
                apply_model_result(
                    self.control.session.state, result, main=False
                )
                data.judges.append(JudgeDecision(plan.judge_digest, result))
                continue
            if plan.request is not None:
                answer = await self.control.human(
                    invocation.operation_id, plan, invocation.arguments
                )
                if answer is None:
                    return stopped_result()
                data.decisions.append(
                    HumanDecision(plan.request_digest, answer)
                )
                continue
            if plan.content is None and not plan.effect_call:
                return plan.result
            outcome = await self._effect(ExecuteEffectInput(data, plan))
            if outcome.status == "replan":
                data.decisions.clear()
                data.judges.clear()
                continue
            return outcome.result
        raise ExecutionFailure("Guard preparation did not converge")

    async def _prepare(self, data):
        try:
            return await self.control.run(
                execute(
                    "ua.prepare_tool",
                    data,
                    PreparedTool,
                    queue=self.start.resource_queue,
                    operation_id=f"{data.tool.invocation.operation_id}:prepare:{data.sequence}",
                )
            )
        except (asyncio.CancelledError, ExecutionFailure):
            if not self.control.stopped:
                raise
            return PreparedTool("", result=stopped_result())

    async def _effect(self, value):
        operation_id = value.prepared.tool.invocation.operation_id
        try:
            outcome = await self.control.run(
                execute(
                    "ua.effect",
                    value,
                    EffectResult,
                    queue=self.start.resource_queue,
                    operation_id=f"{operation_id}:effect:{value.prepared.sequence}",
                    effect=True,
                )
            )
        except (asyncio.CancelledError, ExecutionFailure):
            outcome = EffectResult(
                "unknown",
                detail="Effect activity did not return a committed result",
            )
        revision = 0
        while outcome.status == "unknown":
            revision += 1
            outcome = await execute(
                "ua.reconcile",
                value,
                EffectResult,
                queue=self.start.resource_queue,
                operation_id=f"{operation_id}:reconcile:{revision}",
                timeout=30,
            )
            if outcome.status == "unknown":
                value.resolution = await self.control.wait_reconciliation(
                    operation_id, outcome.detail
                )
        return outcome
