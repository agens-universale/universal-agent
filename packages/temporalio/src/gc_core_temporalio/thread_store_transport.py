"""Workflow transport for the native store's async facade."""

import asyncio

from gc_core.execution.contracts import ExecutionFailure, InfrastructureFailure
from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperationMismatch,
    ThreadStoreOwnershipError,
    ThreadStoreRecoveryError,
)
from temporalio import workflow
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
)

from gc_core_temporalio.activity_config import (
    DEFAULT_ACTIVITY_SETTINGS,
    activity_options,
)
from gc_core_temporalio.thread_store_contracts import ThreadStoreReply

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
    def __init__(self, *, task_queue, settings=None):
        self.task_queue = task_queue
        #: Уровень ``activities.thread_store`` (данные с воркера).
        self.settings = settings or DEFAULT_ACTIVITY_SETTINGS["thread_store"]

    async def __call__(self, name, request):
        try:
            return await workflow.execute_activity(
                name,
                request,
                result_type=ThreadStoreReply,
                summary=f"Thread store: {request.operation.kind if request.operation else name.rsplit('.', 1)[-1]}",
                task_queue=self.task_queue,
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                **activity_options(self.settings),
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
