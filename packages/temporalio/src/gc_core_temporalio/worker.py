"""Opt-in native worker: the real AgentExecutor behind Temporal activities.

All three queues belong to this process and one declared workspace. Resource
queues must be unique to this host when MCP or background commands are enabled.
"""

import argparse
import asyncio
import dataclasses
import logging
import os
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


def store_paths(store):
    """Пути хранилища тредов для ``protected_paths``: агент не читает и не
    правит чужие треды.

    Известные хранилища (файлы, SQLite) — по их пути. Своё обязано объявить
    пути методом ``protected_paths()`` (хранилище вне диска воркера, например
    S3, возвращает ``[]``). Без объявления — отказ: молча не защитить
    данные на диске воркера нельзя.
    """
    declared = getattr(store, "protected_paths", None)
    if callable(declared):
        return list(declared())
    if isinstance(store, SQLiteThreadStore):
        return [
            store.path,
            *(
                f"{store.path}{suffix}"
                for suffix in ("-wal", "-shm", "-journal")
            ),
        ]
    if isinstance(store, DurableFileThreadStore):
        return [str(store._dir)]
    raise ValueError(
        f"Хранилище тредов {type(store).__name__} не объявило protected_paths(): "
        "верните пути его данных на диске воркера (или [] — если данных на "
        "диске нет), чтобы агент не мог их читать и править"
    )


def build_runtime(config, *, stores=None):
    """Провайдер контекста и activities. ``stores`` — готовые хранилища тредов
    (объекты); без них — из ``config.thread_stores``."""
    if stores is None:
        stores = build_stores(config)
        paths = config.state_paths()
    else:
        stores = dict(stores)
        paths = []
        for store in stores.values():
            for path in store_paths(store):
                for form in (os.path.abspath(path), os.path.realpath(path)):
                    if form not in paths:
                        paths.append(form)
    provider = ContextProvider(
        profiles=config.profiles,
        stores=stores,
        resource_host=WorkerResourceHost(config.worker_id),
        state_paths=paths,
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


def default_build_id():
    """Номер сборки для Worker Versioning: версия пакета и хэш исходников
    ``gc_core`` и ``gc_core_temporalio`` (правка кода без подъёма версии —
    тоже новая сборка). Для ``WorkerDeploymentVersion(build_id=...)``."""
    from gc_core_temporalio.provider import WorkerVersioningConfig

    return WorkerVersioningConfig().resolved_build_id()


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


async def run_worker(config, *, client=None):
    """Воркер по конфигу: нативные ``Client``/``Worker`` с ``AgentPlugin``.

    По ``Worker`` на каждую различную очередь конфига (workflow, модель,
    ресурсы); до остановки процесса. ``client`` — уже подключённый (TLS,
    авторизация — на стороне приложения); без него — ``Client.connect`` по
    ``address``/``namespace`` конфига.
    """
    from gc_core_temporalio.plugin import AgentPlugin

    plugin = AgentPlugin.from_worker_config(config)
    if client is None:
        client = await Client.connect(
            config.address, namespace=config.namespace, plugins=[plugin]
        )
        extra = {}
    else:
        extra = {"plugins": [plugin]}
    deployment = deployment_config(config)
    if deployment is not None:
        extra["deployment_config"] = deployment
    queues = dict.fromkeys(
        [config.workflow_queue, config.model_queue, config.resource_queue]
    )
    log = logging.getLogger(__name__)
    defaults = {"ua-workflow", "ua-model", "ua-resource-local"} & set(queues)
    if defaults:
        # Очереди по умолчанию одинаковы у всех воркеров namespace: два
        # воркера разных рабочих каталогов с ними исполняли бы activities
        # друг друга (в чужом cwd).
        log.warning(
            "Очереди по умолчанию (%s) общие для всех воркеров namespace: "
            "задайте свои workflow_queue/model_queue/resource_queue",
            ", ".join(sorted(defaults)),
        )
    async with AsyncExitStack() as stack:
        for queue in queues:
            await stack.enter_async_context(
                Worker(client, task_queue=queue, **extra)
            )
        if not config.versioning.enabled:
            # Без versioning новый код доигрывает ходы, начатые старым:
            # изменённая логика workflow ломает их replay (NDE).
            log.warning(
                "Worker Versioning выключен: перед выкладкой изменённого "
                "кода workflow дождитесь окончания ходов или включите "
                "versioning.enabled"
            )
        else:
            log.info(
                "Worker Versioning: deployment %s, build %s",
                config.versioning.deployment_name,
                config.versioning.resolved_build_id(),
            )
            if config.versioning.set_current_on_start:
                await set_current_version(client, config)
        await asyncio.Event().wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(WorkerConfig.load(args.config)))


if __name__ == "__main__":
    main()
