"""Opt-in native worker: the real AgentExecutor behind Temporal activities.

All three queues belong to this process and one declared workspace. Resource
queues must be unique to this host when MCP or background commands are enabled.
"""

import argparse
import asyncio
import dataclasses
import logging
from contextlib import AsyncExitStack
from pathlib import Path

from gc_core.execution.resource_host import WorkerResourceHost
from gc_core.thread_store_file_durable import DurableFileThreadStore
from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from gc_core_temporalio.activities import AgentActivities
from gc_core_temporalio.activity_config import resolve_activity_settings
from gc_core_temporalio.child_preparation import ChildActivities
from gc_core_temporalio.child_workflow import SubagentWorkflow
from gc_core_temporalio.maintenance import WorkerMaintenance
from gc_core_temporalio.provider import (
    ContextProvider,
    WorkerConfig,
)
from gc_core_temporalio.public_config import public_config_snapshot
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore
from gc_core_temporalio.thread_store_activities import ThreadStoreActivities
from gc_core_temporalio.workflow import AgentWorkflow


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


def build_runtime(config):
    stores = build_stores(config)
    provider = ContextProvider(
        profiles=config.profiles,
        stores=stores,
        resource_host=WorkerResourceHost(config.worker_id),
        state_paths=config.state_paths(),
    )
    native = AgentActivities(
        context_provider=provider,
        stores=stores,
        public_config=public_config_snapshot,
        activity_settings=resolve_activity_settings(config.activities),
    )
    return provider, native, ThreadStoreActivities(stores)


def workflow_runner():
    """Песочница кода workflow: умолчания SDK и запрет сетевых клиентов.

    Модель, инструменты и MCP исполняются в activities; HTTP- или MCP-клиент
    в коде workflow — значит, ядро пошло мимо порта (новый ввод-вывод без
    activity). Умолчания SDK уже запрещают файлы, процессы и часы; сетевые
    клиенты пропускаются как есть (``sandbox_passthrough``), поэтому их
    создание запрещено здесь явно. Только во время исполнения: модуль ядра
    создаёт клиент при импорте, и это не ход.
    """
    from temporalio.worker.workflow_sandbox import (
        SandboxedWorkflowRunner,
        SandboxMatcher,
        SandboxRestrictions,
    )

    def runtime_use(*names):
        return SandboxMatcher(use=set(names), only_runtime=True)

    clients = SandboxMatcher(
        children={
            # httpx.Client запрещать нельзя: ядро наследует его при импорте
            # (наследовать класс под ограничением песочница не умеет), а
            # синхронный клиент ядра — это его ``_CompatClient``.
            "httpx": runtime_use("AsyncClient"),
            "gc_core": SandboxMatcher(
                children={
                    "utils": SandboxMatcher(
                        children={
                            "http_session": runtime_use(
                                "_CompatClient", "NO_NETRC_SESSION"
                            )
                        }
                    )
                }
            ),
            "mcp": SandboxMatcher(
                children={
                    "client": SandboxMatcher(
                        children={
                            "session": runtime_use("ClientSession"),
                            "stdio": runtime_use("stdio_client"),
                            "sse": runtime_use("sse_client"),
                            "streamable_http": runtime_use(
                                "streamablehttp_client",
                                "streamable_http_client",
                            ),
                        }
                    )
                }
            ),
        }
    )
    restrictions = dataclasses.replace(
        SandboxRestrictions.default,
        invalid_module_members=(
            SandboxRestrictions.invalid_module_members_default | clients
        ),
    )
    return SandboxedWorkflowRunner(restrictions=restrictions)


def worker_options(config, native, threads):
    """Coalesce equal queues while preserving a single resource context owner."""
    queues = {}
    children = ChildActivities(native)
    for queue, workflows, activities in (
        (
            config.workflow_queue,
            [AgentWorkflow, SubagentWorkflow],
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
        if workflows:
            entry["workflow_runner"] = workflow_runner()
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
        identity="gc-temporal-worker",
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
    provider, native, threads = build_runtime(config)
    try:
        client = await Client.connect(
            config.address, namespace=config.namespace
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
            await WorkerMaintenance(
                client,
                provider,
                provider.stores,
            ).serve()
    finally:
        await provider.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(WorkerConfig.load(args.config)))


if __name__ == "__main__":
    main()
