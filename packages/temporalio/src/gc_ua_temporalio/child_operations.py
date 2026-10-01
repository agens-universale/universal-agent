"""Sequential child workflows use the same loop and return an explicit delta."""

import asyncio
import json
from dataclasses import replace

from gc_core.execution.contracts import ModelResult, StateDelta, ToolResult
from gc_core.execution.reducers import apply_model_result
from gc_core.usage.account import CATEGORY_SUBAGENT, UsageAccount
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ChildWorkflowError

from gc_ua_temporalio.child_contracts import ChildPlan, PrepareChild
from gc_ua_temporalio.contracts import ModelActivityInput, ToolActivityInput
from gc_ua_temporalio.control_contracts import JudgeDecision
from gc_ua_temporalio.operations import execute


async def invoke_child(operations, invocation):
    session = operations.control.session
    data = PrepareChild(
        operations.owner,
        ToolActivityInput(
            operations.start.profile,
            operations.start.profile_revision,
            operations.owner,
            invocation,
        ),
        operations.start,
        session.state.to_execution_snapshot(),
    )
    for sequence in range(1, 9):
        if operations.control.stopped:
            return _stopped_child()
        data.sequence = sequence
        plan = await execute(
            "ua.prepare_child",
            data,
            ChildPlan,
            queue=operations.start.resource_queue,
        )
        if plan.result:
            return plan.result
        if plan.judge is None:
            if operations.control.stopped:
                return _stopped_child()
            return await _run_child(operations, invocation, plan.start)
        try:
            result = await operations.control.run(
                execute(
                    "ua.model",
                    ModelActivityInput(
                        plan.model_profile,
                        plan.model_revision,
                        operations.owner,
                        replace(
                            plan.judge,
                            operation_id=f"{invocation.operation_id}:role:{sequence}",
                        ),
                    ),
                    ModelResult,
                    queue=operations.start.model_queue,
                )
            )
        except asyncio.CancelledError:
            return _stopped_child()
        apply_model_result(session.state, result, main=False)
        data.judges.append(JudgeDecision(plan.judge_digest, result))
    return ToolResult(
        '{"ok":false,"error":{"code":"role_generation_failed"}}',
        outcome="tool_error",
    )


def _stopped_child():
    return ToolResult(
        '{"ok":false,"error":{"code":"user_stop"}}', outcome="tool_error"
    )


async def _run_child(operations, invocation, start):
    session = operations.control.session
    child_id = f"{workflow.info().workflow_id}/child/{invocation.operation_id}"
    trace = {
        "workflow_id": child_id,
        "thread_id": start.thread_id,
        "system_id": start.system_id,
        "call_id": invocation.call_id,
    }
    session.projection.meta.setdefault("child_runs", []).append(trace)
    await session._persist([])
    trace = session.projection.meta["child_runs"][-1]
    try:
        handle = await workflow.start_child_workflow(
            "AgentSessionWorkflow",
            start,
            id=child_id,
            task_queue=workflow.info().task_queue,
            result_type=dict,
            retry_policy=RetryPolicy(maximum_attempts=1),
            # UNKNOWN effects may need operator reconciliation. Closing a parent
            # must not terminate that reconciliation or lose the child's trace.
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        session.active_child = child_id
        if operations.control.stopped:
            await handle.signal("parent_stop")
        result = await handle
    except ChildWorkflowError:
        trace["outcome"] = "failed"
        return ToolResult(
            json.dumps(
                {
                    "ok": False,
                    "error": {"code": "subagent_run_failed"},
                    "meta": trace,
                }
            ),
            outcome="tool_error",
        )
    finally:
        session.active_child = None
    session.state.usage.merge(
        UsageAccount.from_meta(result["usage"]), as_category=CATEGORY_SUBAGENT
    )
    session.state.tool_calls += result["tool_calls"]
    disposition = result.get("disposition", "PROCESSED")
    trace = session.projection.meta["child_runs"][-1]
    trace.update(
        outcome=disposition, usage=result["usage"], steps=result["steps"]
    )
    return ToolResult(
        json.dumps(
            {
                "ok": disposition == "PROCESSED",
                "result": result["answer"],
                "meta": trace,
            },
            ensure_ascii=False,
        ),
        outcome="ok" if disposition == "PROCESSED" else "tool_error",
        delta=StateDelta(working_memory=result["memory"]),
    )
