"""External operations of the real core executor. No agent loop lives here."""

import asyncio
import json
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

from gc_core.constants.tool_names import EXEC_FETCH_URL_NAME
from gc_core.context_preparation import PreparedRunContext
from gc_core.execution.contracts import (
    ExecutionFailure,
    ModelOperationError,
    ModelRequest,
    ToolEffectRequest,
    ToolResult,
)
from gc_core.execution.guard_contracts import (
    GuardProbeRequest,
)
from gc_core.execution.local import LocalOperations
from gc_core.execution.resources import PromptResourceRequest
from gc_core.project_memory.operations import (
    ProjectMemoryReply,
    ProjectMemoryRequest,
)
from gc_core.run_preparation import PreparedPrompts, PromptPreparationRequest
from gc_core.settings import settings_of
from gc_core.state_preparation import PreparedState, StatePreparationRequest
from gc_core.thread_store_async import athread_store
from gc_core.thread_store_durable import durable_store_capability
from temporalio import activity
from temporalio.converter import value_to_type
from temporalio.exceptions import ApplicationError

from gc_core_temporalio.activity_config import DEFAULT_ACTIVITY_SETTINGS
from gc_core_temporalio.contracts import (
    ActivityCall,
    GuardProbes,
    ModelReply,
    PreparedContext,
)
from gc_core_temporalio.message_refs import (
    CONTEXT_MISSING,
    decode,
    is_ref,
    message_hash,
    refs_in,
)
from gc_core_temporalio.sizes import cut_to_payload, payload_size

#: Потолок вывода инструмента через границу activity (байты UTF-8). Результат
#: activity — запись истории Temporal (лимит 2 МБ). Встроенные инструменты
#: ограничивают вывод сами (``exec_command`` — 40 000 символов); потолок
#: нужен ответам MCP-серверов. Модель видит до
#: ``agent.tool_output_inline_max_chars``, остальное листает через
#: ``read_stored_tool_output`` — в пределах этого потолка.
TOOL_OUTPUT_LIMIT_BYTES = 1024 * 1024


def cap_tool_output(result, limit=TOOL_OUTPUT_LIMIT_BYTES):
    """``ToolResult`` с выводом не тяжелее ``limit`` в истории Temporal
    (``payload_size``: кириллица — 6 байт на символ) и пометкой обрезки."""
    output = result.output if isinstance(result.output, str) else ""
    total = payload_size(output)
    if total <= limit:
        return result
    kept = cut_to_payload(output, limit)
    result.output = (
        f"{kept}\n[вывод обрезан воркером Temporal: показано {len(kept)} из "
        f"{len(output)} символов — лимит записи истории]"
    )
    return result


#: Память воркера под кэш страниц ``fetch_url`` всех сессий (байты JSON).
FETCH_CACHE_LIMIT_BYTES = 64 * 1024 * 1024


class FetchPageCache:
    """Кэш страниц ``fetch_url`` по сессиям — в памяти воркера.

    Локально кэш лежит в ``state.shared`` и пишется в meta треда. Через
    Temporal он шёл бы во вход и результат каждой activity ``fetch_url`` и в
    checkpoint субагента — а страница бывает в мегабайты при лимите записи
    2 МБ. Поэтому workflow кэш не видит, а воркер держит его по сессии
    (``session_key``: субагент делит кэш с родителем, как локально).
    Кэш — только ускорение: после перезапуска воркера или вытеснения
    ``fetch_url`` скачает страницу заново. Сессии вытесняются по давности,
    пока сумма не уложится в ``limit_bytes``.
    """

    def __init__(self, limit_bytes=FETCH_CACHE_LIMIT_BYTES):
        self.limit_bytes = limit_bytes
        self._sessions = OrderedDict()
        self._size = 0

    @staticmethod
    def session_key(call):
        """Сессия вызова: workflow ID корневого workflow (стабилен через
        Continue-As-New, кэш живёт между ходами, как в meta локально); у
        субагента — переданный ему ``child_context["session_id"]``."""
        return (call.child_context or {}).get(
            "session_id"
        ) or activity.info().workflow_id

    def get(self, key):
        entry = self._sessions.get(key)
        if entry is None:
            return []
        self._sessions.move_to_end(key)
        return entry[0]

    def put(self, key, pages):
        old = self._sessions.pop(key, None)
        if old is not None:
            self._size -= old[1]
        size = len(json.dumps(pages, ensure_ascii=False, default=str).encode())
        if not pages or size > self.limit_bytes:
            return
        self._sessions[key] = (pages, size)
        self._size += size
        while self._size > self.limit_bytes:
            _, (_, dropped) = self._sessions.popitem(last=False)
            self._size -= dropped


#: Владельцев хода с сообщениями в памяти воркера (``message_refs``).
_MESSAGE_CACHE_OWNERS = 256

#: Тип ошибки модельной activity, которую Temporal повторяет по политике.
TRANSIENT_MODEL_FAILURE = "ModelProviderTransient"

#: Коды, при которых повтор того же запроса имеет смысл.
_TRANSIENT_STATUSES = {408, 425, 429}


def is_transient_provider_failure(failure) -> bool:
    """Сбой провайдера, который проходит сам: транспорт, 5xx, 408/425/429.

    Не транзиентны: отказ по существу (4xx), авторизация, переполнение
    контекста, отказ из-за картинок, открытый предохранитель алиаса,
    вложенный отказ с кодом провайдера (``insufficient_quota``), а также
    транспорт, который повтор не исправит: неразрешимое имя, сертификат TLS,
    неверный URL (``details["permanent"]``).
    """
    if failure is None or failure.kind != "provider":
        return False
    if (
        failure.auth_error
        or failure.context_overflow
        or failure.image_rejection
        or failure.image_request
        or failure.response_cache_miss
    ):
        return False
    details = failure.details or {}
    if (
        "alias" in details
        or details.get("raw_code") is not None
        or details.get("permanent")
    ):
        return False
    if details.get("transport"):
        return True  # обрыв, таймаут, отказ соединения
    if "status_code" not in details:
        # Неизвестный сбой провайдера: повтор без ограничения не включаем.
        return False
    status = details.get("status_code")
    if status is None:
        # Ошибка в стриме или пустой выбор без кода вовсе (код с текстом —
        # отсечён выше по ``raw_code``): транзиентна, как в ядре
        # (``is_retriable_backend_error``).
        return True
    try:
        status = int(status)
    except (TypeError, ValueError):
        return False
    return status >= 500 or status in _TRANSIENT_STATUSES


class AgentActivities:
    """Dependency injection uses real RunContexts and the selected ThreadStore.

    ``context_provider(call)`` resolves a pinned configuration and resources on
    the worker. It is not a replacement ThreadSession or agent implementation.
    """

    def __init__(
        self,
        *,
        context_provider,
        stores,
        public_config,
        activity_settings=None,
    ):
        self._operation_locks = {}
        #: Владелец хода → {хэш: сообщение}, полученные activity модели
        #: (``message_refs``). Снимается с освобождением контекста хода.
        self._messages = OrderedDict()
        #: Страницы ``fetch_url`` по сессиям (``FetchPageCache``).
        self.fetch_pages = FetchPageCache()
        #: Итоговые таймауты и повторы activities (``resolve_activity_settings``)
        #: — уходят в workflow с подготовкой хода.
        self.activity_settings = activity_settings or DEFAULT_ACTIVITY_SETTINGS
        self.context_provider = context_provider
        self.stores = {
            name: athread_store(store) for name, store in stores.items()
        }
        self.public_config = public_config
        for store in self.stores.values():
            if durable_store_capability(store) is None:
                raise ValueError(
                    "Temporal requires a durable ThreadStore capability"
                )

    @asynccontextmanager
    async def _operation_scope(self, call):
        # Concurrent redelivery in this worker waits for the original attempt.
        key = (call.owner_id, call.operation_id)
        entry = self._operation_locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1

        async def heartbeat():
            while True:
                activity.heartbeat(call.operation_id)
                await asyncio.sleep(2)

        heartbeat_task = (
            asyncio.create_task(heartbeat())
            if activity.in_activity()
            else None
        )
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._operation_locks.pop(key, None)
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _recorded(
        self, call, result_type, handler, *, resolve_context=True, effect=False
    ):
        """Одна операция: контекст воркера, обработчик, перенос ошибок.

        Квитанций нет: повтор activity исполняет операцию заново («хотя бы
        раз», как в Temporal). Эффект инструмента защищён политикой — одна
        попытка по умолчанию (``activity_config``).
        """
        del result_type, effect  # тип результата сериализует Temporal
        async with self._operation_scope(call):
            if call.schema_version != 1:
                raise ApplicationError(
                    "Unsupported activity call", non_retryable=True
                )
            try:
                ctx = (
                    await self.context_provider(call)
                    if resolve_context
                    else None
                )
                return await handler(call, ctx)
            except ExecutionFailure as exc:
                raise ApplicationError(
                    str(exc), type=type(exc).__name__, non_retryable=True
                ) from exc
            except ApplicationError:
                raise
            except Exception as exc:
                # Доменная ошибка (как в local): переносим тип и текст
                # значением, workflow поднимет то же исключение, и ядро
                # обработает его штатно.
                raise ApplicationError(
                    str(exc),
                    type(exc).__name__,
                    type="DomainError",
                    non_retryable=True,
                ) from exc

    @activity.defn(name="agent.context.prepare")
    async def prepare(self, call: ActivityCall) -> PreparedContext:
        return await self._recorded(call, PreparedContext, self._prepare)

    async def _prepare(self, call, ctx):
        from gc_core.run_preparation import load_run_prompts
        from gc_core.tools.subagents.registry import (
            resolve_subagent_definitions,
        )

        check = getattr(self.context_provider, "check_settings", None)
        if check is not None:
            check(call.profile, call.data.get("settings_fingerprint"))
        store = self.stores[call.data["store_id"]]
        public = self.public_config(ctx)
        prompts_request = PromptPreparationRequest.from_config(
            ctx, verbose=bool(settings_of(ctx).agent.verbose)
        )
        prompts = await asyncio.to_thread(
            load_run_prompts, prompts_request, logger=ctx.logger
        )
        return PreparedContext(
            context=PreparedRunContext.from_context(
                ctx, settings=public.settings, runtime=public.runtime
            ),
            subagent_definitions=resolve_subagent_definitions(ctx),
            tool_metadata={
                name: {
                    **{
                        key: value
                        for key, value in entry.items()
                        if key != "tool_fabric"
                    },
                    "effect_is_read_only": bool(
                        getattr(
                            entry.get("tool_fabric"),
                            "effect_is_read_only",
                            False,
                        )
                    ),
                }
                for name, entry in ctx.tools_data.items()
            },
            store_capabilities=[
                name
                for name in (
                    "replace_messages",
                    "archive_and_replace",
                    "reset_run_flags",
                )
                if callable(getattr(store, "a" + name, None))
            ],
            threads_dir=str(getattr(store, "threads_dir", "")),
            resource_owner=(await self.context_provider.resource_owner(call))
            if callable(getattr(self.context_provider, "resource_owner", None))
            else None,
            model_transport_configured=public.model_transport_configured,
            provider_currency=public.provider_currency,
            skills=await ctx.skills_registry.list_skills(),
            prompts_request=prompts_request,
            prompts=prompts,
            activity_settings=self.activity_settings,
        )

    @activity.defn(name="agent.state.prepare")
    async def prepare_state(self, call: ActivityCall) -> PreparedState:
        return await self._recorded(call, PreparedState, self._prepare_state)

    async def _prepare_state(self, call, ctx):
        from gc_core.state_preparation import prepare_state

        request = value_to_type(StatePreparationRequest, call.data)
        return await prepare_state(
            workspace_config=ctx,
            workspace=ctx.workspace,
            os_backend=ctx.os_backend,
            logger=ctx.logger,
            **asdict(request),
        )

    @activity.defn(name="agent.prompts.prepare")
    async def prepare_prompts(self, call: ActivityCall) -> PreparedPrompts:
        return await self._recorded(
            call, PreparedPrompts, self._prepare_prompts
        )

    async def _prepare_prompts(self, call, ctx):
        from gc_core.run_preparation import load_run_prompts

        return await asyncio.to_thread(
            load_run_prompts,
            value_to_type(PromptPreparationRequest, call.data),
            logger=ctx.logger,
        )

    @activity.defn(name="agent.prompt.read")
    async def prompt(self, call: ActivityCall) -> str:
        return await self._recorded(call, str, self._prompt)

    async def _prompt(self, call, ctx):
        from gc_core.resource_loading import load_prompt_resource

        return await asyncio.to_thread(
            load_prompt_resource,
            value_to_type(PromptResourceRequest, call.data),
            ctx=ctx,
        )

    @activity.defn(name="agent.skills.list")
    async def skills(self, call: ActivityCall) -> list[dict]:
        return await self._recorded(call, list[dict], self._skills)

    async def _skills(self, _call, ctx):
        return await ctx.skills_registry.list_skills()

    @activity.defn(name="agent.model.request")
    async def model(self, call: ActivityCall) -> ModelReply:
        return await self._recorded(call, ModelReply, self._model)

    async def _model(self, call, ctx):
        from copy import copy

        from gc_core_temporalio.model_error_encoding import (
            encode_model_failure,
        )
        from gc_core_temporalio.model_routing import (
            materialize_router_identity,
        )

        request = value_to_type(
            ModelRequest, await self._resolve_message_refs(call)
        )
        model_ctx = copy(ctx)
        router = materialize_router_identity(call.data.get("routing"), ctx)
        model_ctx.model_router = router
        router.breaker.replace_from(call.data["circuit_breaker"])
        try:
            result = await LocalOperations(model_ctx).model(request)
        except ModelOperationError as exc:
            result = exc.result
            result.failure = encode_model_failure(exc.cause)
            if call.child_context is None and is_transient_provider_failure(
                result.failure
            ):
                # Обрыв, недоступность, 5xx, 429: повторит Temporal по политике
                # activities.model — ход ждёт провайдера, а не падает. Запрос
                # субагента остаётся данными: его fallback на модель родителя
                # решается в workflow.
                raise ApplicationError(
                    result.failure.message,
                    result.failure.error_type,
                    type=TRANSIENT_MODEL_FAILURE,
                ) from exc
        return ModelReply(result, router.breaker.snapshot())

    @activity.defn(name="agent.tool.execute")
    async def tool(self, call: ActivityCall) -> ToolResult:
        return await self._recorded(
            call, ToolResult, self._tool, effect=call.data.get("effect", False)
        )

    async def _tool(self, call, ctx):
        from gc_core.execution.tool_effects import execute_native_tool_effect

        request = value_to_type(
            ToolEffectRequest,
            {k: v for k, v in call.data.items() if k != "effect"},
        )
        metadata = ctx.tools_data.get(request.name, {})
        live = metadata.get("execution_kind") == "mcp" or (
            request.name == "exec_command"
            and request.arguments.get("run_in_background", False)
        )
        if live:
            host = getattr(self.context_provider, "resource_host", None)
            owner = call.resource_owner
            if owner is None:
                ensure = getattr(
                    self.context_provider, "ensure_resource_owner", None
                )
                if ensure is not None:
                    owner = await ensure(call)
            if host is None or owner is None:
                raise ExecutionFailure("Live tool requires a resource owner")
            return cap_tool_output(
                (
                    await host.execute(
                        owner,
                        request,
                        context_id=call.owner_id
                        if call.child_context
                        else None,
                    )
                ).result
            )
        if request.name != EXEC_FETCH_URL_NAME:
            return cap_tool_output(
                await execute_native_tool_effect(request, ctx=ctx)
            )
        session = self.fetch_pages.session_key(call)
        request.state.fetch_url_cache = self.fetch_pages.get(session)
        result = await execute_native_tool_effect(request, ctx=ctx)
        if result.delta.fetch_url_cache is not None:
            self.fetch_pages.put(session, result.delta.fetch_url_cache)
            result.delta.fetch_url_cache = None
        return cap_tool_output(result)

    @activity.defn(name="agent.memory.execute")
    async def memory(self, call: ActivityCall) -> ProjectMemoryReply:
        # Чтение памяти повторяемо, как и решил workflow; запись — эффект.
        return await self._recorded(
            call,
            ProjectMemoryReply,
            self._memory,
            effect=call.data.get("action") != "read",
        )

    async def _memory(self, call, ctx):
        from gc_core.tools.project.project_memory import ProjectMemory

        state = SimpleNamespace(project_memory_content_hash=None)
        return await asyncio.to_thread(
            ProjectMemory(ctx=ctx, state=state).execute_io,
            value_to_type(ProjectMemoryRequest, call.data),
        )

    @activity.defn(name="agent.memory.hash")
    async def memory_hash(self, call: ActivityCall) -> str | None:
        return await self._recorded(call, str | None, self._memory_hash)

    async def _memory_hash(self, _call, ctx):
        from gc_core.project_memory import get_hash

        return await asyncio.to_thread(get_hash, ctx)

    @activity.defn(name="agent.guard.probe")
    async def guard(self, call: ActivityCall) -> GuardProbes:
        return await self._recorded(call, GuardProbes, self._guard)

    async def _guard(self, call, ctx):
        from gc_core.security.guard_probe import evaluate_guard_probe

        requests = [
            value_to_type(GuardProbeRequest, value)
            for value in (
                call.data["request"],
                *call.data.get("companions", []),
            )
        ]

        def evaluate():
            return [
                evaluate_guard_probe(request, settings=ctx)
                for request in requests
            ]

        primary, *companions = await asyncio.to_thread(evaluate)
        return GuardProbes(primary, companions)

    @activity.defn(name="agent.summary.trace")
    async def summary_trace(self, call: ActivityCall) -> bool:
        return await self._recorded(call, bool, self._summary_trace)

    async def _summary_trace(self, call, ctx):
        from gc_core.summarize.summary_logging import (
            SummaryTraceRecord,
            aappend_summary_trace_record,
        )

        record = value_to_type(SummaryTraceRecord, call.data)
        # Контекст воркера собран без `summary_trace_sink`, так что запись
        # идёт в локальный summary_trace*.jsonl, а не обратно в activity.
        await aappend_summary_trace_record(
            ctx,
            trigger_reason=record.trigger_reason,
            trace_data=record.trace_data,
            newly_summarized_message_id=record.newly_summarized_message_id,
        )
        return True

    @activity.defn(name="agent.context.close")
    async def close(self, call: ActivityCall) -> bool:
        return await self._recorded(
            call,
            bool,
            self._close,
            resolve_context=not callable(
                getattr(self.context_provider, "close", None)
            ),
        )

    async def _close(self, call, ctx):
        close = getattr(self.context_provider, "close", None)
        if close is not None:
            await close(call)
        else:
            from gc_core.agent_executor import _aclose_mcp_and_release
            from gc_core.mcp_session_owner import set_owner

            await _aclose_mcp_and_release(set_owner(ctx.mcp_session_owner))
        return True

    @activity.defn(name="agent.context.release")
    async def release(self, call: ActivityCall) -> bool:
        return await self._recorded(
            call, bool, self._release, resolve_context=False
        )

    async def _release(self, call, _ctx):
        release = getattr(self.context_provider, "release", None)
        if release is not None:
            await release(call)
        self._messages.pop(call.owner_id, None)
        return True

    async def _resolve_message_refs(self, call):
        """Вход запроса к модели со ссылками → полные сообщения.

        Ссылки ищутся в памяти воркера (сообщения прошлых запросов этого
        хода), затем в треде ThreadStore. Не найдено — ``ModelContextMissing``:
        workflow повторит запрос, передав сообщения целиком.
        """
        data = call.data["request"]
        state = dict(data.get("state") or {})
        batch = data.get("messages") or []
        known = self._messages.setdefault(call.owner_id, {})
        self._messages.move_to_end(call.owner_id)
        while len(self._messages) > _MESSAGE_CACHE_OWNERS:
            self._messages.popitem(last=False)
        tools = (
            state.get("tools") if isinstance(state.get("tools"), list) else []
        )
        for message in [*(state.get("messages") or []), *batch, *tools]:
            if not is_ref(message):
                known[message_hash(message)] = deepcopy(message)
        refs = refs_in(state.get("messages")) + refs_in(batch) + refs_in(tools)
        if any(digest not in known for digest in refs):
            await self._load_thread_messages(call, known)
        missing = [digest for digest in refs if digest not in known]
        if missing:
            raise ApplicationError(
                f"Model context is not available on this worker: "
                f"{len(missing)} message(s)",
                type=CONTEXT_MISSING,
                non_retryable=True,
            )
        if "messages" in state:
            state["messages"] = decode(state["messages"], known)
        if tools:
            state["tools"] = decode(tools, known)
        return {**data, "state": state, "messages": decode(batch, known)}

    async def _load_thread_messages(self, call, known):
        """Сообщения треда сессии — в память ссылок (промах кэша воркера)."""
        thread = call.data.get("thread") or {}
        store = self.stores.get(thread.get("store_id"))
        if store is None or not thread.get("thread_id"):
            return
        capability = durable_store_capability(store)
        if capability is None:
            return
        snapshot = await capability[0](
            thread.get("system_id"), thread["thread_id"]
        )
        if snapshot is None or snapshot.state is None:
            return
        for message in snapshot.state.messages:
            known.setdefault(message_hash(message), deepcopy(message))
