"""Весь ход SDK и обработка его результата в одной обычной activity.

Каждая попытка заново запускает агента с сохранённой историей треда.
Внутренние вызовы инструментов не являются activities и не восстанавливаются
из истории Temporal. RetryPolicy выбирает приложение: повтор может повторить
внешние действия. AgentPlugin для этого режима не нужен.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from gc_core.aio import AsyncAgent
from gc_core.config_modules.root import AppConfig
from gc_core.settings import LoadedSettings, settings_of
from gc_core.trace_id import normalize_trace_id
from temporalio import activity
from temporalio.exceptions import ApplicationError

from gc_core_temporalio.turn_activity_contracts import (
    AgentActivityRequest,
    AgentActivityResult,
)

Postprocess = Callable[[AgentActivityRequest, Any], Awaitable[Any]]


class AgentActivity:
    """Регистрируйте ``run`` в нативном ``Worker(activities=[runner.run])``.

    ``agents`` — профили SDK на воркере; хранилище тредов общее для реплик.
    ``postprocess`` выполняется на том же процессе, до завершения activity
    (например, выгружает файлы в S3). Его ошибка повторяет ВЕСЬ запуск, если
    RetryPolicy разрешает повтор. Callback должен возвращать JSON-данные.

    Heartbeat работает от подготовки MCP до конца postprocess. На вызове
    задайте heartbeat_timeout больше heartbeat_interval и общий таймаут.
    Рабочий каталог — cwd процесса: объект никогда не вызывает chdir.
    Для неразделённого workspace ограничьте Worker одной активной activity.
    Хранилищем владеет приложение и закрывает его после остановки воркера.
    """

    def __init__(
        self,
        agents: Mapping[str, AppConfig | LoadedSettings],
        *,
        thread_store: Any,
        postprocess: Postprocess | None = None,
        heartbeat_interval: float = 10,
    ):
        if not agents or thread_store is None:
            raise ValueError("AgentActivity requires agents and thread_store")
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        self.agents = dict(agents)
        self.thread_store = thread_store
        self.postprocess = postprocess
        self.heartbeat_interval = heartbeat_interval
        self.workspace = Path.cwd().resolve()

    def _settings(self, request):
        try:
            source = self.agents[request.agent]
            trace = normalize_trace_id(request.trace_id)
            if not request.thread_id or not request.system_id:
                raise ValueError("thread_id and system_id must not be empty")
        except (KeyError, ValueError) as exc:
            raise ApplicationError(
                str(exc),
                type="InvalidAgentActivityRequest",
                non_retryable=True,
            ) from exc
        if Path.cwd().resolve() != self.workspace:
            raise ApplicationError("Worker cwd changed", non_retryable=True)
        loaded = (
            source
            if isinstance(source, LoadedSettings)
            else LoadedSettings(settings=settings_of(source))
        )
        return loaded.with_runtime(
            launch_root=str(self.workspace),
            human_channel=False,
            trace_id=trace,
        )

    async def _heartbeat(self):
        while True:
            activity.heartbeat()
            await asyncio.sleep(self.heartbeat_interval)

    @activity.defn(name="universal_agent.run")
    async def run(self, request: AgentActivityRequest) -> AgentActivityResult:
        """Одна попытка: подготовить SDK, выполнить ход, обработать результат."""
        settings = self._settings(request)
        activity.heartbeat()
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            async with await AsyncAgent.create(
                settings, thread_store=self.thread_store
            ) as agent:
                answer = await agent.run(
                    request.prompt,
                    thread_id=request.thread_id,
                    system=request.system_id,
                )
                output = (
                    await self.postprocess(request, answer)
                    if self.postprocess is not None
                    else None
                )
                return AgentActivityResult(answer, output)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
