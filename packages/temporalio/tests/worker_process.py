"""Crash gates wrap production native wiring only in this test process."""

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from dataclasses import asdict
from pathlib import Path

from gc_core.execution.contracts import ToolResult
from gc_core_temporalio.contracts import ModelReply
from gc_core_temporalio.provider import WorkerConfig
from gc_core_temporalio.worker import build_runtime
from gc_core_temporalio.thread_store_activities import ThreadStoreActivities
from gc_core_temporalio.thread_store_contracts import (
    ThreadStoreCall,
    ThreadStoreReply,
)
from restart_interceptor import RestartInterceptor
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker
from live_support import worker_kwargs, worker_queues


async def gate(kind, value=None):
    if sys.argv[2] == kind:
        Path(sys.argv[3]).write_text(
            json.dumps({"kind": kind, "value": value})
        )
        await asyncio.Event().wait()


class GatedThreadActivities(ThreadStoreActivities):
    @activity.defn(name="universal_agent.thread.commit")
    async def commit(self, request: ThreadStoreCall) -> ThreadStoreReply:
        selected = request.operation.kind == "persist"
        if selected:
            await gate("before_store_commit", asdict(request))
        result = await super().commit(request)
        if selected:
            await gate(
                "after_store_receipt",
                {"request": asdict(request), "receipt": asdict(result)},
            )
        return result


async def main():
    config = WorkerConfig.load(sys.argv[1])
    provider, native, _threads = build_runtime(config)
    threads = GatedThreadActivities(provider.stores)
    original_model, original_recorded = native._model, native._recorded

    async def model(call, ctx):
        await gate("before_model_request", asdict(call))
        result = await original_model(call, ctx)
        await gate("after_model_response", asdict(call))
        return result

    async def recorded(call, result_type, handler, **kwargs):
        if result_type is ModelReply:
            await gate("before_model_activity", asdict(call))
        result = await original_recorded(call, result_type, handler, **kwargs)
        if result_type is ModelReply:
            await gate("after_model_activity", asdict(call))
        if result_type is ToolResult:
            await gate(
                "after_tool_activity",
                {"call": asdict(call), "result": asdict(result)},
            )
        return result

    native._model, native._recorded = model, recorded
    client = await Client.connect(config.address, namespace=config.namespace)
    try:
        async with AsyncExitStack() as stack:
            for queue in worker_queues(config):
                await stack.enter_async_context(
                    Worker(
                        client,
                        **worker_kwargs(config, native, threads, queue=queue),
                        interceptors=[RestartInterceptor()],
                    )
                )
            await asyncio.Event().wait()
    finally:
        await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
