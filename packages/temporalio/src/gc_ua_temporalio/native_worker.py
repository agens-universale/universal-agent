"""Opt-in native worker: the real AgentExecutor behind Temporal activities.

All three queues belong to this process and one declared workspace. Resource
queues must be unique to this host when MCP or background commands are enabled.
"""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path

from gc_core.execution.resource_host import WorkerResourceHost
from gc_core.thread_store_file_durable import DurableFileThreadStore
from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from gc_ua_temporalio.native_activities import NativeActivities
from gc_ua_temporalio.native_activity_config import resolve_activity_settings
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


def build_stores(config):
    """Хранилища тредов из конфига воркера (их же открывает клиент unlock).

    Файловые треды, чей коммит прервало падение процесса, доводятся до конца
    сразу: иначе их видел бы незавершёнными список тредов до первого
    обращения к каждому.
    """
    from gc_core.thread_store_journal import recover_pending_threads

    stores = {}
    for name, spec in config.thread_stores.items():
        if spec.kind == "file":
            if Path(spec.path).is_dir():
                recover_pending_threads(spec.path)
            stores[name] = DurableFileThreadStore(spec.path)
        else:
            stores[name] = SQLiteThreadStore(spec.path)
    return stores


def build_native_runtime(config):
    stores = build_stores(config)
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
        activity_settings=resolve_activity_settings(config.activities),
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
    deployment = deployment_config(config)
    if deployment is not None:
        for entry in queues.values():
            entry["deployment_config"] = deployment
    return list(queues.values())


def deployment_config(config):
    """``WorkerDeploymentConfig`` при включённом Worker Versioning."""
    versioning = config.versioning
    if not versioning.enabled:
        return None
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(
            deployment_name=versioning.deployment_name,
            build_id=versioning.resolved_build_id(),
        ),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.PINNED,
    )


async def set_current_version(client, config, *, attempts=50, delay=0.2):
    """Объявить сборку воркера текущей (``versioning.set_current_on_start``).

    Версия появляется на сервере после первого опроса очередей, поэтому
    первые попытки могут не найти её — повторяем недолго.
    """
    from temporalio.api.workflowservice.v1 import (
        SetWorkerDeploymentCurrentVersionRequest,
    )
    from temporalio.service import RPCError

    versioning = config.versioning
    request = SetWorkerDeploymentCurrentVersionRequest(
        namespace=client.namespace,
        deployment_name=versioning.deployment_name,
        build_id=versioning.resolved_build_id(),
        identity="gc-temporal-native-worker",
    )
    for attempt in range(attempts):
        try:
            await (
                client.workflow_service.set_worker_deployment_current_version(
                    request
                )
            )
            return
        except RPCError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(delay)


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
            if not config.versioning.enabled:
                # Без versioning новый код доигрывает ходы, начатые старым:
                # изменённая логика workflow ломает их replay (NDE).
                logging.getLogger(__name__).warning(
                    "Worker Versioning выключен: перед выкладкой изменённого "
                    "кода workflow дождитесь окончания ходов или включите "
                    "versioning.enabled"
                )
            else:
                logging.getLogger(__name__).info(
                    "Worker Versioning: deployment %s, build %s",
                    config.versioning.deployment_name,
                    config.versioning.resolved_build_id(),
                )
                if config.versioning.set_current_on_start:
                    await set_current_version(client, config)
            # Брошенные контексты (terminate посреди хода) и служебные данные
            # закрытых сессий — фоном, пока работает воркер.
            await NativeMaintenance(
                client,
                provider,
                native.receipts,
                provider.stores,
                payloads=config.payload_storage,
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
