"""Opt-in native worker: the real AgentExecutor behind Temporal activities.

All three queues belong to this process and one declared workspace. Resource
queues must be unique to this host when MCP or background commands are enabled.
"""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack

from gc_core.execution.resource_host import WorkerResourceHost
from gc_core.thread_store_file_durable import DurableFileThreadStore
from temporalio.client import Client
from temporalio.worker import Worker

from gc_ua_temporalio.native_activities import NativeActivities
from gc_ua_temporalio.native_child_preparation import NativeChildActivities
from gc_ua_temporalio.native_child_workflow import NativeSubagentWorkflow
from gc_ua_temporalio.native_maintenance import NativeMaintenance
from gc_ua_temporalio.native_provider import (
    NativeContextProvider,
    NativeWorkerConfig,
)
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from gc_ua_temporalio.payload_storage import data_converter
from gc_ua_temporalio.public_config import public_config_snapshot
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from gc_ua_temporalio.storage.technical import TechnicalStore
from gc_ua_temporalio.thread_store_activities import ThreadStoreActivities


def build_native_runtime(config):
    stores = {
        name: (
            DurableFileThreadStore(spec.path)
            if spec.kind == "file"
            else SQLiteThreadStore(spec.path)
        )
        for name, spec in config.thread_stores.items()
    }
    receipts = TechnicalStore(config.receipts_database)
    provider = NativeContextProvider(
        profiles=config.profiles,
        stores=stores,
        receipts=receipts,
        resource_host=WorkerResourceHost(config.worker_id),
        state_paths=config.state_paths(),
    )
    native = NativeActivities(
        context_provider=provider,
        stores=stores,
        receipts=receipts,
        public_config=public_config_snapshot,
        preview_profiles={
            name
            for name, profile in config.profiles.items()
            if profile.preview_stream
        },
    )
    return provider, native, ThreadStoreActivities(stores)


def worker_options(config, native, threads):
    """Coalesce equal queues while preserving a single resource context owner."""
    queues = {}
    children = NativeChildActivities(native)
    for queue, workflows, activities in (
        (
            config.workflow_queue,
            [NativeAgentWorkflow, NativeSubagentWorkflow],
            [],
        ),
        (config.model_queue, [], [native.model]),
        (
            config.resource_queue,
            [],
            [
                children.prepare,
                native.prepare,
                native.prepare_state,
                native.prepare_prompts,
                native.prompt,
                native.skills,
                native.tool,
                native.guard,
                native.memory,
                native.memory_hash,
                native.summary_trace,
                native.close,
                native.release,
                threads.load,
                threads.commit,
                threads.read_trace,
            ],
        ),
    ):
        entry = queues.setdefault(
            queue, {"task_queue": queue, "workflows": [], "activities": []}
        )
        entry["workflows"].extend(workflows)
        entry["activities"].extend(activities)
    return list(queues.values())


async def serve(config):
    provider, native, threads = build_native_runtime(config)
    try:
        client = await Client.connect(
            config.address,
            namespace=config.namespace,
            data_converter=data_converter(config.payload_storage),
        )
        async with AsyncExitStack() as stack:
            for options in worker_options(config, native, threads):
                await stack.enter_async_context(Worker(client, **options))
            # Брошенные контексты (terminate посреди хода) и служебные данные
            # закрытых сессий — фоном, пока работает воркер.
            await NativeMaintenance(
                client, provider, native.receipts, provider.stores
            ).serve()
    finally:
        await provider.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(NativeWorkerConfig.load(args.config)))


if __name__ == "__main__":
    main()
