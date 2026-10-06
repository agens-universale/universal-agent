"""Crash gates wrap production native wiring only in this test process."""

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from dataclasses import asdict
from pathlib import Path

from gc_core.execution.contracts import ToolResult
from gc_ua_temporalio.native_contracts import NativeModelReply
from gc_ua_temporalio.native_provider import NativeWorkerConfig
from gc_ua_temporalio.native_worker import build_native_runtime, worker_options
from gc_ua_temporalio.thread_store_activities import ThreadStoreActivities
from gc_ua_temporalio.thread_store_contracts import (
    ThreadStoreCall,
    ThreadStoreReply,
)
from native_restart_interceptor import RestartInterceptor
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker


async def gate(kind, value=None):
    if sys.argv[2] == kind:
        Path(sys.argv[3]).write_text(
            json.dumps({"kind": kind, "value": value})
        )
        await asyncio.Event().wait()


class GatedThreadActivities(ThreadStoreActivities):
    @activity.defn(name="agent.thread.commit")
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
    config = NativeWorkerConfig.load(sys.argv[1])
    provider, native, _threads = build_native_runtime(config)
    threads = GatedThreadActivities(provider.stores)
    original_model, original_recorded = native._model, native._recorded

    async def model(call, ctx):
        await gate("before_model_request", asdict(call))
        result = await original_model(call, ctx)
        await gate("after_model_response", asdict(call))
        return result

    async def recorded(call, result_type, handler, **kwargs):
        if result_type is NativeModelReply:
            await gate("before_model_activity", asdict(call))
        result = await original_recorded(call, result_type, handler, **kwargs)
        if result_type is NativeModelReply:
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
            for options in worker_options(config, native, threads):
                await stack.enter_async_context(
                    Worker(
                        client,
                        **options,
                        interceptors=[RestartInterceptor()],
                    )
                )
            await asyncio.Event().wait()
    finally:
        await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
