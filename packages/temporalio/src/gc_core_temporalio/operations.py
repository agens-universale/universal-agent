"""Temporal transport for native core resource ports."""

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta

from gc_core.constants.tool_names import EXEC_COMMAND_NAME, EXEC_PYTHON_NAME
from gc_core.context_preparation import settings_from_snapshot
from gc_core.execution.contracts import (
    ExecutionFailure,
    GuardPreconditionChanged,
    InfrastructureFailure,
    ModelRequestStopped,
    OutcomeUnknown,
    ToolOutcomeUnknown,
    ToolResult,
    TurnFailure,
)
from gc_core.execution.resource_contracts import ResourceOwnerLost
from gc_core.project_memory.operations import ProjectMemoryReply
from gc_core.run_preparation import PreparedPrompts
from gc_core.state_preparation import PreparedState
from temporalio import workflow
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    FailureError,
)
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

from gc_core_temporalio.activity_config import (
    DEFAULT_ACTIVITY_SETTINGS,
    activity_options,
)
from gc_core_temporalio.contracts import (
    ActivityCall,
    GuardProbes,
    ModelReply,
)
from gc_core_temporalio.message_refs import CONTEXT_MISSING, SentMessages
from gc_core_temporalio.model_routing import public_router_identity

#: Доменные исключения, которые activity переносит значением (``DomainError``)
#: и которые workflow поднимает тем же типом — ядро обрабатывает их, как в local.
_DOMAIN_ERRORS = {
    cls.__name__: cls
    for cls in (
        ValueError,
        TypeError,
        KeyError,
        LookupError,
        IndexError,
        RuntimeError,
        NotImplementedError,
        OSError,
        FileNotFoundError,
        FileExistsError,
        PermissionError,
        IsADirectoryError,
        NotADirectoryError,
        TimeoutError,
        GuardPreconditionChanged,
        ToolOutcomeUnknown,
        ResourceOwnerLost,
    )
}
_DOMAIN_ERRORS.update(
    {
        "McpSessionLostError": ToolOutcomeUnknown,
        "UnicodeDecodeError": ValueError,
        "JSONDecodeError": ValueError,
    }
)

TOOL_RETRIES_EXHAUSTED = (
    "Инструмент не выполнен: сбой исполнителя и после повторов. Вызов можно "
    "повторить."
)

#: Потолок activity по умолчанию; инструмент с долгим ``timeout`` получает
#: свой (``timeout`` + запас), иначе Temporal обрывал бы его на 15 минутах.
ACTIVITY_TIMEOUT = timedelta(minutes=15)
TOOL_TIMEOUT_MARGIN = timedelta(minutes=5)
#: Верхняя граница: огромный ``timeout`` модели не должен давать переполнение
#: или Duration за пределами protobuf (вечный сбой workflow task).
TOOL_TIMEOUT_CAP = timedelta(hours=24)
#: Инструменты с ``timeout`` по умолчанию ``agent.exec_timeout_sec``.
_EXEC_TOOLS = frozenset({EXEC_COMMAND_NAME, EXEC_PYTHON_NAME})

#: Ход продолжается без этих activities только для эффекта инструмента:
#: сбой исполнителя после единственной попытки становится ошибкой инструмента.
INTERRUPTED_EFFECT = (
    "Исполнение прервано сбоем исполнителя; неизвестно, успела ли операция "
    "выполниться. Проверьте состояние, прежде чем повторять."
)


class _ModelContextMissing(Exception):
    """Воркер не нашёл сообщений по ссылкам (``message_refs``)."""


def session_failure(exc):
    """Доменный отказ сессии — ``ApplicationError``, run становится FAILED.

    Доменный — ``TurnFailure`` ядра (``ExecutionFailure``, ошибка провайдера,
    переполнение контекста), ошибка, поднятая из исхода
    activity/child workflow (в цепочке ``__cause__`` есть ``FailureError``),
    или восстановленный исход модельной операции (``restored_model_failure``).
    Всё остальное — сбой кода workflow: недетерминизм после деплоя, запрет
    песочницы, баг. Тогда ``None``: падает только workflow task, run ждёт
    исправленного воркера и продолжает ход, а не теряет его.
    """
    if isinstance(exc, ApplicationError):
        return exc
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        if isinstance(current, (TurnFailure, FailureError)) or getattr(
            current, "restored_model_failure", False
        ):
            return ApplicationError(
                str(exc) or type(exc).__name__,
                type=type(exc).__name__,
                non_retryable=True,
            )
        seen.add(id(current))
        current = current.__cause__
    return None


def restore_domain_error(cause):
    """Rebuild a domain exception transported as ``DomainError``."""
    name = cause.details[0] if cause.details else "RuntimeError"
    return _DOMAIN_ERRORS.get(name, RuntimeError)(cause.message)


def activity_summary(name, data):
    """Readable UI labels without prompts, file contents or credentials."""
    if name == "agent.model.request":
        request = data["request"]
        return f"Model: {request['request_label']} ({request['consumer']})"
    if name == "agent.tool.execute":
        return f"Tool: {data['name']}"
    if name == "agent.memory.execute":
        return f"Project memory: {data['action']} / {data['phase']}"
    if name == "agent.guard.probe":
        return f"Guard: {data['request']['kind']}"
    return name.removeprefix("agent.").replace(".", " / ")


class WorkflowOperations:
    def __init__(self, start, owner_id, new_id):
        self.start, self.owner_id, self.new_id = start, owner_id, new_id
        self.router = None
        self.resource_owner = None
        self.child_context = None
        self._prompts = {}
        self.tool_metadata = {}
        #: Снимок подготовки хода (PreparedContext), см. prompt/skills.
        self.prepared = None
        self._probe_cache = {}
        self._settings_cache = None
        self.closed = False
        self.released = False
        #: Таймауты и повторы по уровням; после подготовки хода — с воркера.
        self.activity_settings = DEFAULT_ACTIVITY_SETTINGS
        #: Сообщения, уже переданные activity модели в этом запуске.
        self._sent_messages = SentMessages()
        #: Прокси ThreadStore хода: прочитанные из треда сообщения — ссылки.
        self.thread_store = None
        #: Ожидающая activity модели — её отменяет stop (``stop_model``).
        self._model_task = None
        self._stopped_model_task = None

    def next_operation_id(self, kind):
        return self.new_id(kind)

    def monotonic(self):
        return workflow.time()

    async def call(
        self,
        name,
        data,
        result_type,
        *,
        operation_id=None,
        model=False,
        effect=False,
        tool=False,
        settings=None,
    ):
        """Execute one activity.

        ``effect=True`` — внешний эффект без идемпотентности: одна попытка
        (повторы — только явно, ``activities.tools.<имя>``). Сбой исполнителя
        или лимит времени — модель получает ошибку «исход неизвестен», эффект
        второй раз не исполняется.
        """
        request = ActivityCall(
            self.start.profile,
            self.owner_id,
            operation_id or self.new_id(name),
            data,
            resource_owner=self.resource_owner,
            child_context=self.child_context,
        )
        level = (
            settings
            or self.activity_settings["model" if model else "resource"]
        )
        queue = level.get("task_queue") or (
            self.start.model_queue if model else self.start.resource_queue
        )
        try:
            return await workflow.execute_activity(
                name,
                request,
                result_type=result_type,
                activity_id=request.operation_id,
                summary=activity_summary(name, data),
                task_queue=queue,
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                **activity_options(level, effect=effect),
            )
        except ActivityError as exc:
            raise self._activity_error(
                exc, name, effect=effect, tool=tool
            ) from exc

    @staticmethod
    def _activity_error(exc, name, *, effect, tool=False):
        cause = exc.cause
        if isinstance(cause, CancelledError):
            return asyncio.CancelledError(str(cause))
        if effect and isinstance(cause, TemporalTimeoutError):
            return ToolOutcomeUnknown(
                "Исполнение превысило лимит времени activity и прервано; "
                "неизвестно, успела ли операция выполниться. Проверьте "
                "состояние, прежде чем повторять."
            )
        if isinstance(cause, ApplicationError):
            if cause.type == "DomainError":
                return restore_domain_error(cause)
            failures = {
                cls.__name__: cls
                for cls in (
                    ExecutionFailure,
                    InfrastructureFailure,
                    OutcomeUnknown,
                )
            }
            if cause.type in failures:
                return failures[cause.type](cause.message)
            if cause.type == CONTEXT_MISSING:
                return _ModelContextMissing(cause.message)
            if cause.type == "ModelProviderTransient":
                # Исчерпан заданный в activities.model лимит повторов.
                return InfrastructureFailure(
                    f"Model provider unavailable: {cause.message}"
                )
        if effect:
            return ToolOutcomeUnknown(INTERRUPTED_EFFECT)
        if tool:
            # Чтение не удалось и после повторов: ошибка инструмента, модель
            # может повторить или обойтись, сессия не падает (как в local).
            return RuntimeError(TOOL_RETRIES_EXHAUSTED)
        return InfrastructureFailure(f"Native activity failed: {name}")

    async def model(self, request):
        provider = getattr(self, "router_provider", None)
        router = provider() if provider is not None else self.router
        self.router = router
        task = asyncio.ensure_future(self._request_model(request, router))
        self._model_task = task
        try:
            reply = await task
        except asyncio.CancelledError:
            # Отмену workflow не глотаем: только отмена от stop.
            if self._stopped_model_task is not task:
                raise
            # Отменено stop'ом: activity могла повторять сбой провайдера
            # сколько угодно — ход завершается как остановленный.
            raise ModelRequestStopped("Ход остановлен") from None
        finally:
            self._model_task = None
        router.breaker.replace_from(reply.circuit_breaker)
        return reply.result

    async def _request_model(self, request, router):
        """Activity модели: уже переданные сообщения — ссылками
        (``message_refs``). Воркер без них — повтор с сообщениями целиком."""
        data = asdict(request)
        thread = {
            "store_id": self.start.store_id,
            "system_id": self.start.system_id,
            "thread_id": data["state"].get("thread_id")
            or self.start.thread_id,
        }
        operation_id = request.operation_id
        store = self.thread_store
        stored = (
            store.message_hashes.get(
                (thread["system_id"], thread["thread_id"]), set()
            )
            if store is not None
            else set()
        )
        for attempt in range(2):
            if attempt:
                stored = set()
            state = dict(data["state"])
            state["messages"], inline = self._sent_messages.encode(
                state.get("messages"), also=stored
            )
            batch, inline_batch = self._sent_messages.encode(
                data["messages"], also={*stored, *inline}
            )
            inline_tools = []
            if isinstance(state.get("tools"), list):
                # Схемы инструментов одинаковы на всех шагах хода.
                state["tools"], inline_tools = self._sent_messages.encode(
                    state["tools"]
                )
            try:
                reply = await self.call(
                    "agent.model.request",
                    {
                        "request": {**data, "state": state, "messages": batch},
                        "circuit_breaker": router.breaker.snapshot(),
                        "routing": public_router_identity(router),
                        "thread": thread,
                    },
                    ModelReply,
                    operation_id=operation_id,
                    model=True,
                )
            except _ModelContextMissing:
                if attempt:
                    raise InfrastructureFailure(
                        "Model context is not available on the worker"
                    ) from None
                # Воркер без этих сообщений (перезапуск, другой процесс, а
                # в треде их нет): один повтор, всё целиком.
                self._sent_messages.forget()
                operation_id = f"{request.operation_id}/full"
                continue
            self._sent_messages.confirm(
                [*inline, *inline_batch, *inline_tools]
            )
            return reply

    def stop_model(self):
        """stop: отменить ожидающую activity модели (и её повторы).

        Следующие запросы не блокируются: stop между шагами ловит цикл, а
        постобработка остановленного хода (``format_text``) идёт, как в local.
        """
        task = self._model_task
        if task is not None and not task.done():
            self._stopped_model_task = task
            task.cancel()

    async def tool_effect(self, request):
        metadata = self.tool_metadata.get(request.name, {})
        effect = not metadata.get("effect_is_read_only", False)
        data = asdict(request)
        # Кэш страниц ``fetch_url`` живёт в памяти воркера
        # (``activities.FetchPageCache``): страница бывает в мегабайты,
        # а вход и результат activity — записи истории (лимит 2 МБ).
        data["state"]["fetch_url_cache"] = []
        return await self.call(
            "agent.tool.execute",
            {**data, "effect": effect},
            ToolResult,
            operation_id=request.operation_id,
            effect=effect,
            tool=True,
            settings=self._tool_settings(request),
        )

    def _tool_settings(self, request):
        """Уровень инструмента: ``activities.tools.<имя>`` или ``resource``;
        лимит времени без явного — от ``timeout`` инструмента."""
        settings = dict(
            self.activity_settings.get("tools", {}).get(request.name)
            or self.activity_settings["resource"]
        )
        explicit = request.name in self.activity_settings.get("tools", {})
        if not (explicit and settings.get("start_to_close") is not None):
            settings["start_to_close"] = max(
                self._tool_timeout(request).total_seconds(),
                settings.get("start_to_close") or 0,
            )
        return settings

    def _tool_timeout(self, request):
        """Лимит activity инструмента: его ``timeout`` (у команд без него —
        умолчание ``agent.exec_timeout_sec``) плюс запас, от 15 минут до суток."""
        seconds = request.arguments.get("timeout")
        if not isinstance(seconds, int) and request.name in _EXEC_TOOLS:
            settings = self._prepared_settings()
            seconds = settings.agent.exec_timeout_sec if settings else None
        if not isinstance(seconds, int) or seconds <= 0:
            return ACTIVITY_TIMEOUT
        seconds = min(seconds, int(TOOL_TIMEOUT_CAP.total_seconds()))
        return max(
            ACTIVITY_TIMEOUT,
            timedelta(seconds=seconds) + TOOL_TIMEOUT_MARGIN,
        )

    def _probe_companions(self, request):
        """Проверки, которые guard сделает сразу после этой: одним походом.

        Защищённый путь проверяется до write/read guard того же пути. Если
        запрос ядра не совпадёт с предсказанным, кэш просто не сработает.
        """
        settings = self._prepared_settings()
        if settings is None:
            return []
        security = settings.security
        if request.kind == "protected_write" and security.write_guard.enabled:
            return [
                replace(request, kind="write_path", operation=operation)
                for operation in ("write", "append", "search_and_replace")
            ]
        if request.kind == "protected_read" and security.read_guard.enabled:
            return [replace(request, kind="read_path", operation="")]
        return []

    def _prepared_settings(self):
        """Настройки из снимка подготовки хода (None — подготовки не было).

        Собираются один раз на снимок: проверка guard идёт на каждый тул.
        """
        prepared = self.prepared
        if prepared is None:
            return None
        cached = self._settings_cache
        if cached is None or cached[0] is not prepared:
            loaded = settings_from_snapshot(
                prepared.context.settings, prepared.context.runtime
            )
            cached = self._settings_cache = (prepared, loaded.settings)
        return cached[1]

    async def guard_probe(self, request):
        cached = self._probe_cache.pop(request, None)
        if cached is not None:
            return cached
        companions = self._probe_companions(request)
        reply = await self.call(
            "agent.guard.probe",
            {
                "request": asdict(request),
                "companions": [asdict(item) for item in companions],
            },
            GuardProbes,
        )
        self._probe_cache = dict(zip(companions, reply.companions))
        return reply.primary

    async def state_preparer(self, request):
        return await self.call(
            "agent.state.prepare", asdict(request), PreparedState
        )

    async def prompt_loader(self, request):
        prepared = self.prepared
        if prepared is not None and prepared.prompts_request == request:
            return deepcopy(prepared.prompts)
        return await self.call(
            "agent.prompts.prepare", asdict(request), PreparedPrompts
        )

    async def prompt_resource(self, request):
        if request not in self._prompts:
            self._prompts[request] = await self.call(
                "agent.prompt.read", asdict(request), str
            )
        return self._prompts[request]

    async def skills_loader(self):
        prepared = self.prepared
        if prepared is not None and prepared.skills is not None:
            return deepcopy(prepared.skills)
        return await self.call("agent.skills.list", {}, list[dict])

    async def project_memory_io(self, request):
        # Чтение памяти повторяемо; запись — эффект не более одного раза.
        return await self.call(
            "agent.memory.execute",
            asdict(request),
            ProjectMemoryReply,
            effect=request.action != "read",
        )

    async def project_memory_hash(self):
        return await self.call("agent.memory.hash", {}, str | None)

    async def summary_trace_sink(self, record):
        await self.call("agent.summary.trace", asdict(record), bool)

    async def mcp_cleanup(self):
        if not self.closed:
            await self.call("agent.context.close", {}, bool)
            self.closed = True

    async def release_context(self):
        if not self.released:
            await self.call("agent.context.release", {}, bool)
            self.released = True
