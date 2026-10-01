"""Workflow transport for the native store's async facade."""

import asyncio
from datetime import timedelta

from gc_core.execution.contracts import ExecutionFailure, InfrastructureFailure
from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperationMismatch,
    ThreadStoreOwnershipError,
    ThreadStoreRecoveryError,
)
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
)

from gc_ua_temporalio.thread_store_contracts import ThreadStoreReply

_STORE_ERRORS = {
    cls.__name__: cls
    for cls in (
        ExecutionFailure,
        ThreadStoreConflict,
        ThreadStoreOwnershipError,
        ThreadStoreOperationMismatch,
        ThreadStoreRecoveryError,
    )
}


class ThreadStoreTransport:
    def __init__(self, *, task_queue, timeout_seconds=120):
        self.task_queue = task_queue
        self.timeout_seconds = timeout_seconds

    async def __call__(self, name, request):
        try:
            return await workflow.execute_activity(
                name,
                request,
                result_type=ThreadStoreReply,
                summary=f"Thread store: {request.operation.kind if request.operation else name.rsplit('.', 1)[-1]}",
                task_queue=self.task_queue,
                start_to_close_timeout=timedelta(seconds=self.timeout_seconds),
                retry_policy=RetryPolicy(maximum_attempts=5),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
        except ActivityError as exc:
            cause = exc.cause
            if isinstance(cause, CancelledError):
                raise asyncio.CancelledError(str(cause)) from exc
            if (
                isinstance(cause, ApplicationError)
                and cause.type in _STORE_ERRORS
            ):
                raise _STORE_ERRORS[cause.type](cause.message) from exc
            raise InfrastructureFailure("ThreadStore activity failed") from exc
