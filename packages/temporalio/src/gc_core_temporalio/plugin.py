"""``AgentPlugin``: агент ядра в воркере Temporal, как ``PydanticAIPlugin``.

Клиент и воркер создаёт приложение нативными классами temporalio
(``Client.connect``, ``Worker``, ``WorkerDeploymentConfig``); плагин добавляет
воркеру workflow сессии и субагента, activities агента и песочницу кода
workflow. Своё в плагине — только понятия агента: какие агенты (их настройки
— те же, что получает ``AsyncAgent.create``), рабочий каталог и хранилище
тредов (объектом). Таймауты и повторы activities — нативные
``temporalio.workflow.ActivityConfig`` с ``RetryPolicy``.

    plugin = AgentPlugin(
        agents={"shop": load_settings("shop.yaml")},
        thread_store=DurableFileThreadStore("/var/ua/threads"),
        model_activity=ActivityConfig(start_to_close_timeout=timedelta(minutes=10)),
    )
    client = await Client.connect("localhost:7233", plugins=[plugin])
    async with Worker(client, task_queue="shop"):
        ...

Все activities идут в очередь сессии; запросы к модели можно вынести на
отдельные воркеры: ``model_activity=ActivityConfig(task_queue="ua-model")`` и
второй ``Worker`` с этим плагином на ``ua-model``. Живые ресурсы (MCP-сессии,
фоновые команды) принадлежат процессу: очередь сессии при них должна быть
своей у каждого процесса воркера.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from pathlib import Path
from typing import Any, Mapping

from temporalio.plugin import SimplePlugin

from gc_core_temporalio.activity_config import (
    ActivitiesConfig,
    from_activity_config,
)
from gc_core_temporalio.child_preparation import ChildActivities
from gc_core_temporalio.child_workflow import UniversalSubagentWorkflow
from gc_core_temporalio.maintenance import WorkerMaintenance
from gc_core_temporalio.provider import AgentProfile, WorkerConfig
from gc_core_temporalio.worker import build_runtime, workflow_runner
from gc_core_temporalio.workflow import UniversalAgentWorkflow

logger = logging.getLogger(__name__)

#: Имя хранилища тредов, если оно одно (``thread_store=``).
DEFAULT_STORE = "default"


class AgentPlugin(SimplePlugin):
    """Плагин воркера: workflow и activities агентов ядра.

    ``agents`` — имя агента → его настройки: ``LoadedSettings`` (как из
    ``load_settings``, вместе с рантаймом), ``AppConfig`` или готовый
    ``AgentProfile``. Имя — то, что клиент передаёт в ``RunRequest.profile``
    (``TemporalDurability(agent=...)``); менять его у живых сессий нельзя.

    ``thread_store`` — хранилище тредов (объект с durable-контрактом:
    ``DurableFileThreadStore``, ``SQLiteThreadStore`` или своё, проверенное
    ``gc_testkit.durable_thread_store_conformance``); ``thread_stores`` —
    несколько по именам. ``workspace`` — рабочий каталог агентов (по
    умолчанию cwd): воркер должен быть запущен в нём, chdir он не делает.

    ``activity`` / ``model_activity`` / ``resource_activity`` /
    ``thread_store_activity`` / ``tool_activities`` — нативные
    ``ActivityConfig``: поверх умолчаний пакета (модель повторяется без
    ограничения, эффект инструмента — одна попытка).
    """

    def __init__(
        self,
        agents: Mapping[str, Any],
        *,
        thread_store: Any = None,
        thread_stores: Mapping[str, Any] | None = None,
        workspace: str | os.PathLike | None = None,
        worker_id: str | None = None,
        activity: Any = None,
        model_activity: Any = None,
        resource_activity: Any = None,
        thread_store_activity: Any = None,
        tool_activities: Mapping[str, Any] | None = None,
        log_handlers: Any = (),
        human_input: bool = False,
        _activities: ActivitiesConfig | None = None,
    ) -> None:
        stores = dict(thread_stores or {})
        if thread_store is not None:
            if DEFAULT_STORE in stores:
                raise ValueError(
                    f"thread_store и thread_stores[{DEFAULT_STORE!r}] заданы оба"
                )
            stores[DEFAULT_STORE] = thread_store
        if not stores:
            raise ValueError(
                "AgentPlugin: нужен thread_store (хранилище тредов)"
            )
        if not agents:
            raise ValueError("AgentPlugin: нужен хотя бы один агент")
        root = str(Path(workspace or os.getcwd()).resolve())
        default_store = (
            DEFAULT_STORE if DEFAULT_STORE in stores else next(iter(stores))
        )
        self.config = WorkerConfig(
            worker_id=worker_id or f"{socket.gethostname()}-{os.getpid()}",
            human_input=human_input,
            thread_stores={},
            profiles={
                name: _profile(spec, root, default_store)
                for name, spec in agents.items()
            },
            activities=_activities
            or ActivitiesConfig(
                default=from_activity_config(activity),
                model=from_activity_config(model_activity),
                resource=from_activity_config(resource_activity),
                thread_store=from_activity_config(thread_store_activity),
                tools={
                    name: from_activity_config(value, tool=True)
                    for name, value in (tool_activities or {}).items()
                },
            ),
        )
        self.provider, self.activities_host, self.thread_activities = (
            build_runtime(
                self.config, stores=stores, log_handlers=log_handlers
            )
        )
        native, threads = self.activities_host, self.thread_activities
        children = ChildActivities(native)
        self._client = None
        self._runs = 0
        self._maintenance = None
        super().__init__(
            "gc_core_temporalio.AgentPlugin",
            workflows=[UniversalAgentWorkflow, UniversalSubagentWorkflow],
            activities=[
                native.model,
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
            workflow_runner=_keep_user_runner,
            run_context=self._run_context,
        )

    @classmethod
    def from_worker_config(cls, config: WorkerConfig, stores=None):
        """Плагин из JSON-конфига воркера (``gc-temporal-worker --config``):
        те же профили, хранилища и секция ``activities``."""
        from gc_core_temporalio.worker import build_stores

        return cls(
            dict(config.profiles),
            thread_stores=stores
            if stores is not None
            else build_stores(config),
            worker_id=config.worker_id,
            _activities=config.activities,
        )

    @property
    def stores(self) -> dict[str, Any]:
        """Хранилища тредов по именам (``store_id`` сессий)."""
        return dict(self.provider.stores)

    def configure_worker(self, config):
        # Клиент воркера нужен фоновому обслуживанию (describe сессий).
        self._client = config.get("client")
        return super().configure_worker(config)

    @contextlib.asynccontextmanager
    async def _run_context(self):
        """Вокруг ``Worker.run``: фоновое обслуживание (брошенные контексты,
        старые служебные записи) и закрытие ресурсов агентов в конце. Один
        плагин на нескольких воркерах — одно обслуживание."""
        self._runs += 1
        if self._runs == 1 and self._client is not None:
            self._maintenance = asyncio.create_task(
                WorkerMaintenance(
                    self._client, self.provider, self.provider.stores
                ).serve()
            )
        try:
            yield
        finally:
            self._runs -= 1
            if self._runs == 0:
                if self._maintenance is not None:
                    self._maintenance.cancel()
                    with contextlib.suppress(BaseException):
                        await self._maintenance
                    self._maintenance = None
                await self.provider.aclose()


def _keep_user_runner(existing):
    """Песочница плагина (запрет сетевых клиентов в коде workflow), если
    приложение не задало свой ``workflow_runner``; свой — не перекрывать
    молча, а предупредить, что защита плагина к нему не применена."""
    from temporalio.worker.workflow_sandbox import (
        SandboxedWorkflowRunner,
        SandboxRestrictions,
    )

    # Worker подставляет SandboxedWorkflowRunner() с умолчаниями SDK, если
    # приложение runner не задавало, — это не «свой» runner.
    if existing is None or (
        type(existing) is SandboxedWorkflowRunner
        and existing.restrictions == SandboxRestrictions.default
    ):
        return workflow_runner()
    logger.warning(
        "Worker с собственным workflow_runner: песочница AgentPlugin (запрет "
        "HTTP- и MCP-клиентов в коде workflow) к нему не применена"
    )
    return existing


def _profile(spec, workspace, default_store):
    if isinstance(spec, AgentProfile):
        return spec
    return AgentProfile(workspace=workspace, store_id=default_store, core=spec)


__all__ = ["DEFAULT_STORE", "AgentPlugin"]
