"""Drive the existing summary coordinator, interrupting each LLM call."""

from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

from gc_core.execution.state import ExecutionState
from gc_core.run_summary_coordinator import RunSummaryCoordinator
from gc_core.tools import aapply_tool_result
from temporalio import activity

from gc_ua_temporalio.guard_requests import Decisions, JudgeWait, digest
from gc_ua_temporalio.storage.sqlite import encode
from gc_ua_temporalio.summary_contracts import (
    SUMMARY_FIELDS,
    SummaryPlan,
    SummaryRequest,
)


class SummaryActivities:
    def __init__(self, activities):
        self.activities = activities
        self.runtime = activities.runtime

    @activity.defn(name="ua.prepare_summary")
    async def prepare(self, data: SummaryRequest) -> SummaryPlan:
        return await self.activities._recorded(
            data,
            f"{data.operation_id}:prepare:{data.sequence}",
            SummaryPlan,
            self._prepare,
        )

    async def _prepare(self, data):
        profile, original = self.runtime.profile(data.profile, data.revision)
        state = ExecutionState.from_execution_snapshot(data.state)
        ctx = SimpleNamespace(**vars(original))
        fingerprint = digest(asdict(replace(data, sequence=0, judges=[])))
        ctx.operations = Decisions(fingerprint, (), data.judges)
        events = []

        async def applied(event):
            events.append(event)

        ctx.on_summary_applied = applied
        coordinator = RunSummaryCoordinator(
            ctx=ctx,
            state=state,
            thread_session=None,
            summary_trigger_chars=profile.summary_trigger_chars,
            turn_end_trigger_chars=profile.summary_trigger_chars,
            summary_trigger_tokens=profile.summary_trigger_tokens,
            turn_end_trigger_tokens=profile.summary_trigger_tokens,
            is_cli_mode=False,
            pre_backend_trigger_reason="pre_backend",
            logger=ctx.logger,
        )
        coordinator.on_summary_applied = applied
        messages = deepcopy(data.messages)
        try:
            result = await self._drive(data, coordinator, state, ctx, messages)
        except JudgeWait as wait:
            return SummaryPlan(judge=wait.request, judge_digest=wait.digest)
        if not events:
            return SummaryPlan(result=result)
        snapshot = state.to_execution_snapshot()["data"]
        patch = {
            name: snapshot[name] for name in SUMMARY_FIELDS if name in snapshot
        }
        trace = self.runtime.artifacts.put(
            encode({"owner": data.owner, "events": events}).encode()
        )
        return SummaryPlan(
            patch=patch, messages=messages, trace=trace, result=result
        )

    @staticmethod
    async def _drive(data, coordinator, state, ctx, messages):
        if data.stage == "pre_backend":
            await coordinator.apre_backend_compaction_hook(messages, data.mode)
        elif data.stage == "turn_end":
            await coordinator.aturn_end_compact_if_needed()
        elif data.stage == "force":
            await coordinator.aforce_compact_once(
                trigger_reason="context_overflow"
            )
        elif data.stage == "tool" and data.tool is not None:
            return await aapply_tool_result(
                "session_compact",
                data.tool.arguments,
                ctx=ctx,
                state=state,
                call_id=data.tool.call_id,
            )
        else:
            raise ValueError("Unknown summary stage")
        return None
