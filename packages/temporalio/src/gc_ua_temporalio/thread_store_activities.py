"""Activities delegate to the injected native ThreadStore, without session policy."""

import asyncio
import inspect
from dataclasses import replace

from gc_core.execution.contracts import ExecutionFailure
from gc_core.thread_store_async import athread_store
from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperation,
    durable_store_capability,
)
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from gc_ua_temporalio.thread_store_contracts import (
    ThreadStoreCall,
    ThreadStoreReply,
)


async def _invoke(store, name, *args, **kwargs):
    """Асинхронное хранилище — напрямую; синхронное (файлы, SQLite) — в потоке.

    Иначе блокирующий I/O держит event loop worker'а и задерживает heartbeat
    соседних activities.
    """
    native = getattr(store, "a" + name, None)
    if inspect.iscoroutinefunction(native):
        return await native(*args, **kwargs)
    return await asyncio.to_thread(getattr(store, name), *args, **kwargs)


class ThreadStoreActivities:
    def __init__(self, stores):
        self.raw = dict(stores)
        self.stores = {
            name: athread_store(store) for name, store in stores.items()
        }
        for name, port in self.stores.items():
            if durable_store_capability(port) is None:
                raise ValueError(
                    f"ThreadStore {name!r} needs durable snapshot/commit capability"
                )

    def _port(self, request):
        try:
            return self.raw[request.store_id]
        except KeyError:
            raise ApplicationError(
                "Unknown configured ThreadStore",
                type="ExecutionFailure",
                non_retryable=True,
            ) from None

    @activity.defn(name="agent.thread.load")
    async def load(self, request: ThreadStoreCall) -> ThreadStoreReply:
        store = self._port(request)
        try:
            snapshot = await _invoke(
                store,
                "read_snapshot",
                request.system_id,
                request.thread_id,
                include_state=request.include_state,
            )
            return ThreadStoreReply(snapshot=snapshot)
        except ExecutionFailure as exc:
            raise _application_error(exc) from exc

    @activity.defn(name="agent.thread.commit")
    async def commit(self, request: ThreadStoreCall) -> ThreadStoreReply:
        if request.operation is None:
            raise ApplicationError(
                "Missing ThreadStore operation",
                type="ExecutionFailure",
                non_retryable=True,
            )
        store = self._port(request)
        try:
            if request.operation.kind == "claim_owner":
                receipt = await _claim(store, request)
            else:
                receipt = await _commit(store, request, request.operation)
            return ThreadStoreReply(receipt=receipt)
        except ExecutionFailure as exc:
            raise _application_error(exc) from exc

    @activity.defn(name="agent.thread.read_trace")
    async def read_trace(self, request: ThreadStoreCall) -> ThreadStoreReply:
        try:
            records = await _invoke(
                self._port(request),
                "read_subagent_trace",
                request.system_id,
                request.thread_id,
            )
            return ThreadStoreReply(trace=records)
        except ExecutionFailure as exc:
            raise _application_error(exc) from exc

    def registered(self):
        return [self.load, self.commit, self.read_trace]


async def _commit(store, request, operation):
    return await _invoke(
        store,
        "commit_operation",
        request.system_id,
        request.thread_id,
        operation,
    )


async def _claim(store, request):
    """Захват треда; владельца, чей workflow уже не RUNNING, снять.

    Владелец треда — workflow ID сессии. Упавшая, снятая (terminate) или
    исчезнувшая сессия иначе держала бы тред вечно. Снятие идёт операцией от
    имени старого владельца с его эпохой: новая эпоха отсекает его поздние
    коммиты. Живой чужой владелец — по-прежнему отказ.
    """
    operation = request.operation
    try:
        return await _commit(store, request, operation)
    except ThreadStoreConflict:
        snapshot = await _invoke(
            store,
            "read_snapshot",
            request.system_id,
            request.thread_id,
            include_state=False,
        )
        desired = operation.payload.get("owner_id", operation.owner_id)
        stale = snapshot.owner_id
        if stale not in (None, desired):
            if await _owner_alive(stale):
                raise
            await _commit(
                store,
                request,
                ThreadStoreOperation(
                    f"{operation.operation_id}/release-stale",
                    "release_owner",
                    generation=snapshot.generation,
                    owner_id=stale,
                    owner_epoch=snapshot.owner_epoch,
                ),
            )
            activity.logger.warning(
                "Released thread owner %s: its workflow is not running", stale
            )
        receipt = await _commit(
            store,
            request,
            replace(
                operation,
                operation_id=f"{operation.operation_id}/takeover",
                expected_revision=None,
                generation=snapshot.generation,
            ),
        )
        # Workflow ждёт квитанцию своей операции: захват — её исход.
        return replace(receipt, operation_id=operation.operation_id)


async def _owner_alive(workflow_id, client=None):
    """Workflow владельца ещё исполняется (без клиента — считать живым)."""
    if client is None:
        try:
            client = activity.client()
        except RuntimeError:
            return True
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return False
        # Транзиентный сбой Temporal (UNAVAILABLE, DEADLINE…): повтор activity,
        # а не отказ захвата.
        raise
    return description.status == WorkflowExecutionStatus.RUNNING


async def release_stale_owner(store, system_id, thread_id, client):
    """Снять владельца треда, чей workflow уже не исполняется.

    Для локального TUI/CLI после terminate или падения сессии: иначе тред
    закрыт, пока его не займёт новая сессия. Живой владелец не снимается.
    Возвращает снятого владельца или ``None``.
    """
    import uuid

    snapshot = await _invoke(
        store, "read_snapshot", system_id, thread_id, include_state=False
    )
    owner = snapshot.owner_id
    if owner is None or await _owner_alive(owner, client):
        return None
    await _invoke(
        store,
        "commit_operation",
        system_id,
        thread_id,
        ThreadStoreOperation(
            f"unlock/{uuid.uuid4().hex}",
            "release_owner",
            generation=snapshot.generation,
            owner_id=owner,
            owner_epoch=snapshot.owner_epoch,
        ),
    )
    return owner


def _application_error(exc):
    return ApplicationError(
        str(exc), type=type(exc).__name__, non_retryable=True
    )
