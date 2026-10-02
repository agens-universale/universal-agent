"""Activities delegate to the injected native ThreadStore, without session policy."""

import asyncio
import inspect

from gc_core.execution.contracts import ExecutionFailure
from gc_core.thread_store_async import athread_store
from gc_core.thread_store_durable import durable_store_capability
from temporalio import activity
from temporalio.exceptions import ApplicationError

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
            receipt = await _invoke(
                store,
                "commit_operation",
                request.system_id,
                request.thread_id,
                request.operation,
            )
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


def _application_error(exc):
    return ApplicationError(
        str(exc), type=type(exc).__name__, non_retryable=True
    )
