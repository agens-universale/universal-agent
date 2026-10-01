"""Model steps are activities; applying the prepared patch is replayable."""

from dataclasses import asdict, replace

from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.reducers import apply_model_result
from gc_core.execution.state import ExecutionState

from gc_ua_temporalio.control_contracts import JudgeDecision
from gc_ua_temporalio.operations import execute
from gc_ua_temporalio.summary_contracts import (
    SUMMARY_FIELDS,
    SummaryPlan,
    SummaryRequest,
)


class SessionSummary:
    def __init__(self, engine):
        self.engine = engine

    async def pre_backend(self, messages, mode):
        await self.compact("pre_backend", messages=messages, mode=mode)

    async def turn_end(self):
        await self.compact("turn_end")

    async def force(self):
        await self.compact("force")

    async def compact(self, stage, *, messages=None, mode="", tool=None):
        session, start = self.engine.session, self.engine.start
        if session.control.stopped:
            return None
        operations = session.operations
        operation = operations.next_operation_id("summary")
        data = SummaryRequest(
            start.profile,
            start.profile_revision,
            session.owner,
            operation,
            session.state.to_execution_snapshot(),
            stage,
            messages=messages or [],
            mode=mode,
            tool=tool,
        )
        for sequence in range(1, 33):
            data.sequence = sequence
            plan = await execute(
                "ua.prepare_summary",
                data,
                SummaryPlan,
                queue=start.resource_queue,
                operation_id=f"{operation}:prepare:{sequence}",
            )
            if plan.judge is None:
                await self._apply(plan, messages)
                return plan.result
            result = await operations.model(
                replace(
                    plan.judge, operation_id=f"{operation}:model:{sequence}"
                )
            )
            if session.control.stopped:
                return None
            apply_model_result(session.state, result, main=False)
            data.judges.append(JudgeDecision(plan.judge_digest, result))
        raise ExecutionFailure("Summary exceeded its bounded model budget")

    async def _apply(self, plan, messages):
        if not plan.patch:
            return
        if set(plan.patch) - set(SUMMARY_FIELDS):
            raise ExecutionFailure("Unexpected summary patch field")
        session = self.engine.session
        snapshot = session.state.to_execution_snapshot()
        snapshot["data"].update(plan.patch)
        restored = ExecutionState.from_execution_snapshot(snapshot)
        for field in plan.patch:
            setattr(session.state, field, getattr(restored, field))
        if messages is not None:
            messages[:] = plan.messages
        traces = session.projection.meta.get("summary_traces", [])
        session.projection.meta["summary_traces"] = [
            *traces[-19:],
            asdict(plan.trace),
        ]
        await session._persist(session.state.messages)
