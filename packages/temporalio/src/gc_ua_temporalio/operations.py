"""Each model request and tool invocation is a separate durable command."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from gc_core.execution.contracts import (
    ExecutionFailure,
    ModelResult,
    ProjectionAck,
    ToolResult,
)
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, is_cancelled_exception

from gc_ua_temporalio.contracts import ModelActivityInput, ToolActivityInput
from gc_ua_temporalio.wire import validate_payload

# Bounded stand: before multi-turn/Continue-As-New, reject oversized commands.
RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=3,
)


async def execute(
    name,
    value,
    result_type,
    *,
    queue,
    timeout=240,
    operation_id=None,
    effect=False,
):
    validate_payload(value)
    try:
        return await workflow.execute_activity(
            name,
            value,
            result_type=result_type,
            task_queue=queue,
            activity_id=operation_id,
            start_to_close_timeout=timedelta(seconds=timeout),
            heartbeat_timeout=timedelta(seconds=10)
            if name != "ua.persist"
            else None,
            schedule_to_close_timeout=timedelta(minutes=15),
            retry_policy=RetryPolicy(maximum_attempts=1) if effect else RETRY,
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
            if effect
            else workflow.ActivityCancellationType.TRY_CANCEL,
        )
    except ActivityError as exc:
        # Core must not turn a failed persist or exhausted activity into a bounce.
        raise ExecutionFailure(f"Activity {name} failed") from exc


class TemporalOperations:
    def __init__(self, start, owner):
        self.start = start
        self.owner = owner
        self.sequence = 0

    def next_operation_id(self, kind):
        self.sequence += 1
        return f"{kind}-{self.sequence}"

    def monotonic(self):
        return workflow.time()

    async def model(self, request):
        value = ModelActivityInput(
            self.start.profile,
            self.start.profile_revision,
            self.owner,
            request,
        )
        try:
            result = await execute(
                "ua.model",
                value,
                ModelResult,
                queue=self.start.model_queue,
                operation_id=request.operation_id,
            )
        except ExecutionFailure as exc:
            if exc.__cause__ is not None and is_cancelled_exception(
                exc.__cause__
            ):
                raise asyncio.CancelledError(
                    "Model activity cancelled"
                ) from exc
            raise
        return result

    async def tool(self, invocation):
        value = ToolActivityInput(
            self.start.profile,
            self.start.profile_revision,
            self.owner,
            invocation,
        )
        return await execute(
            "ua.tool",
            value,
            ToolResult,
            queue=self.start.resource_queue,
            operation_id=invocation.operation_id,
        )

    async def persist(self, batch):
        return await execute(
            "ua.persist",
            batch,
            ProjectionAck,
            queue=self.start.resource_queue,
            timeout=30,
            operation_id=batch.event_id,
        )
