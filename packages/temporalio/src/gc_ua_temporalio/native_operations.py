"""Temporal transport for native core resource ports."""

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta

from gc_core.context_preparation import settings_from_snapshot
from gc_core.execution.contracts import (
    ExecutionFailure,
    GuardPreconditionChanged,
    InfrastructureFailure,
    OutcomeUnknown,
    ToolOutcomeUnknown,
    ToolResult,
)
from gc_core.execution.resource_contracts import ResourceOwnerLost
from gc_core.project_memory.operations import ProjectMemoryReply
from gc_core.run_preparation import PreparedPrompts
from gc_core.state_preparation import PreparedState
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    FailureError,
)
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

from gc_ua_temporalio.native_contracts import (
    NativeCall,
    NativeGuardProbes,
    NativeModelReply,
)
from gc_ua_temporalio.native_model_routing import public_router_identity

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

#: Потолок activity по умолчанию; инструмент с долгим ``timeout`` получает
#: свой (``timeout`` + запас), иначе Temporal обрывал бы его на 15 минутах.
ACTIVITY_TIMEOUT = timedelta(minutes=15)
TOOL_TIMEOUT_MARGIN = timedelta(minutes=5)

#: Ход продолжается без этих activities только для эффекта инструмента:
#: сбой исполнителя после единственной попытки становится ошибкой инструмента.
INTERRUPTED_EFFECT = (
    "Исполнение прервано сбоем исполнителя; неизвестно, успела ли операция "
    "выполниться. Проверьте состояние, прежде чем повторять."
)


def session_failure(exc):
    """Доменный отказ сессии — ``ApplicationError``, run становится FAILED.

    Доменный — ``ExecutionFailure`` ядра или ошибка, поднятая из исхода
    activity/child workflow (в цепочке ``__cause__`` есть ``FailureError``).
    Всё остальное — сбой кода workflow: недетерминизм после деплоя, запрет
    песочницы, баг. Тогда ``None``: падает только workflow task, run ждёт
    исправленного воркера и продолжает ход, а не теряет его.
    """
    if isinstance(exc, ApplicationError):
        return exc
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        if isinstance(current, (ExecutionFailure, FailureError)):
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


class NativeOperations:
    def __init__(self, start, owner_id, new_id):
        self.start, self.owner_id, self.new_id = start, owner_id, new_id
        self.router = None
        self.resource_owner = None
        self.child_context = None
        self._prompts = {}
        self.tool_metadata = {}
        #: Снимок подготовки хода (NativePreparedContext), см. prompt/skills.
        self.prepared = None
        self._probe_cache = {}
        self._settings_cache = None
        self.closed = False
        self.released = False

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
        start_to_close=ACTIVITY_TIMEOUT,
    ):
        """Execute one activity.

        ``effect=True`` — внешний эффект без идемпотентности. Вторая попытка
        только читает квитанцию первой и никогда не исполняет эффект заново;
        без квитанции модель получает ошибку «исход неизвестен».
        """
        request = NativeCall(
            self.start.profile,
            self.owner_id,
            operation_id or self.new_id(name),
            data,
            resource_owner=self.resource_owner,
            child_context=self.child_context,
        )
        try:
            return await workflow.execute_activity(
                name,
                request,
                result_type=result_type,
                activity_id=request.operation_id,
                summary=activity_summary(name, data),
                task_queue=self.start.model_queue
                if model
                else self.start.resource_queue,
                start_to_close_timeout=start_to_close,
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2 if effect else 5),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
        except ActivityError as exc:
            raise self._activity_error(exc, name, effect=effect) from exc

    @staticmethod
    def _activity_error(exc, name, *, effect):
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
        if effect:
            return ToolOutcomeUnknown(INTERRUPTED_EFFECT)
        return InfrastructureFailure(f"Native activity failed: {name}")

    async def model(self, request):
        provider = getattr(self, "router_provider", None)
        router = provider() if provider is not None else self.router
        self.router = router
        reply = await self.call(
            "agent.model.request",
            {
                "request": asdict(request),
                "circuit_breaker": router.breaker.snapshot(),
                "routing": public_router_identity(router),
            },
            NativeModelReply,
            operation_id=request.operation_id,
            model=True,
        )
        router.breaker.replace_from(reply.circuit_breaker)
        return reply.result

    async def tool_effect(self, request):
        metadata = self.tool_metadata.get(request.name, {})
        effect = not metadata.get("effect_is_read_only", False)
        return await self.call(
            "agent.tool.execute",
            {**asdict(request), "effect": effect},
            ToolResult,
            operation_id=request.operation_id,
            effect=effect,
            start_to_close=self._tool_timeout(request),
        )

    def _tool_timeout(self, request):
        """Лимит activity инструмента: его ``timeout`` (или умолчание
        ``agent.exec_timeout_sec`` для команд) плюс запас, не меньше 15 минут."""
        seconds = request.arguments.get("timeout")
        if not isinstance(seconds, int) and request.name == "exec_command":
            settings = self._prepared_settings()
            seconds = settings.agent.exec_timeout_sec if settings else None
        if not isinstance(seconds, int) or seconds <= 0:
            return ACTIVITY_TIMEOUT
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
            NativeGuardProbes,
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
        return await self.call(
            "agent.memory.execute",
            asdict(request),
            ProjectMemoryReply,
            effect=True,
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
