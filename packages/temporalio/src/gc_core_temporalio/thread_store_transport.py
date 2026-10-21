"""Транспорт прокси хранилища тредов: вызов activity из кода workflow."""

import asyncio
import dataclasses

from gc_core.execution.contracts import ExecutionFailure, InfrastructureFailure
from gc_core.threads.errors import (
    OperationMismatch,
    OwnershipError,
    ThreadConflict,
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
from gc_core_temporalio.sizes import HistoryLimitExceeded, check_history_size
from gc_core_temporalio.thread_store_contracts import (
    BaseMismatch,
    ThreadChanged,
    ThreadReply,
)

#: Отказы activity, которые доходят до ядра своим типом: отказы контракта
#: хранилища (по типу сессия решает: повторить запись или остановить ход),
#: провода (база разницы, ответ менялся) и хода.
_STORE_ERRORS = {
    cls.__name__: cls
    for cls in (
        ExecutionFailure,
        HistoryLimitExceeded,
        ThreadConflict,
        OwnershipError,
        OperationMismatch,
        BaseMismatch,
        ThreadChanged,
        ValueError,
    )
}


class ThreadStoreTransport:
    def __init__(self, *, task_queue, settings=None):
        self.task_queue = task_queue
        #: Уровень ``activities.thread_store`` (данные с воркера).
        self.settings = settings or DEFAULT_ACTIVITY_SETTINGS["thread_store"]

    async def __call__(self, name, call):
        # Больше записи истории — сервер отвергнет команду, и задача
        # workflow падала бы бесконечно: отказ хода с причиной и размером.
        check_history_size(f"Вход {name}", dataclasses.asdict(call))
        try:
            return await workflow.execute_activity(
                name,
                call,
                result_type=ThreadReply,
                summary=f"Thread store: {name.rsplit('.', 1)[-1]}",
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
