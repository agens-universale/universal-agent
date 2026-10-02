"""External operations of the real core executor. No agent loop lives here."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from types import SimpleNamespace

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

from gc_ua_temporalio.native_contracts import (
    NativeCall,
    NativeGuardProbes,
    NativeModelReply,
    NativePreparedContext,
)
from gc_ua_temporalio.native_operations import INTERRUPTED_EFFECT


def _digest(call):
    return hashlib.sha256(
        json.dumps(asdict(call), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class NativeActivities:
    """Dependency injection uses real RunContexts and the selected ThreadStore.

    ``context_provider(call)`` resolves a pinned configuration and resources on
    the worker. ``receipts`` stores operation results independently of thread
    history. It is not a replacement ThreadSession or agent implementation.
    """

    def __init__(
        self,
        *,
        context_provider,
        stores,
        receipts,
        public_config,
        preview_profiles=(),
    ):
        self._operation_locks = {}
        self.context_provider = context_provider
        self.stores = {
            name: athread_store(store) for name, store in stores.items()
        }
        self.receipts = receipts
        self.public_config = public_config
        self.preview_profiles = frozenset(preview_profiles)
        for store in self.stores.values():
            if durable_store_capability(store) is None:
                raise ValueError(
                    "Native Temporal requires a durable ThreadStore capability"
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
        async with self._operation_scope(call):
            return await self._recorded_once(
                call,
                result_type,
                handler,
                resolve_context=resolve_context,
                effect=effect,
            )

    async def _recorded_once(
        self, call, result_type, handler, *, resolve_context=True, effect=False
    ):
        if call.schema_version != 1:
            raise ApplicationError(
                "Unsupported native call", non_retryable=True
            )
        digest = _digest(call)
        cached = await asyncio.to_thread(
            self.receipts.operation_result,
            call.owner_id,
            call.operation_id,
            digest,
        )
        if cached is not None:
            return value_to_type(result_type, cached)
        if effect and activity.in_activity() and activity.info().attempt > 1:
            # Повтор внешнего эффекта только читает квитанцию: квитанции нет —
            # исход неизвестен, и эффект второй раз не исполняется.
            raise ApplicationError(
                INTERRUPTED_EFFECT,
                "ToolOutcomeUnknown",
                type="DomainError",
                non_retryable=True,
            )
        try:
            ctx = (
                await self.context_provider(call) if resolve_context else None
            )
            result = await handler(call, ctx)
        except ExecutionFailure as exc:
            raise ApplicationError(
                str(exc), type=type(exc).__name__, non_retryable=True
            ) from exc
        except ApplicationError:
            raise
        except Exception as exc:
            # Доменная ошибка (как в local): переносим тип и текст значением,
            # workflow поднимет то же исключение, и ядро обработает его штатно.
            raise ApplicationError(
                str(exc),
                type(exc).__name__,
                type="DomainError",
                non_retryable=True,
            ) from exc
        encoded = (
            asdict(result)
            if hasattr(result, "__dataclass_fields__")
            else result
        )
        saved = await asyncio.to_thread(
            self.receipts.save_operation_result,
            call.owner_id,
            call.operation_id,
            digest,
            encoded,
        )
        return value_to_type(result_type, saved)

    @activity.defn(name="agent.context.prepare")
    async def prepare(self, call: NativeCall) -> NativePreparedContext:
        return await self._recorded(call, NativePreparedContext, self._prepare)

    async def _prepare(self, call, ctx):
        from gc_core.run_preparation import load_run_prompts
        from gc_core.tools.subagents.registry import (
            resolve_subagent_definitions,
        )

        store = self.stores[call.data["store_id"]]
        public = self.public_config(ctx.config_dict)
        config = getattr(public, "config_dict", public)
        prompts_request = PromptPreparationRequest.from_config(
            config, verbose=bool(settings_of(ctx).agent.verbose)
        )
        prompts = await asyncio.to_thread(
            load_run_prompts, prompts_request, logger=ctx.logger
        )
        return NativePreparedContext(
            context=PreparedRunContext.from_context(ctx, config_dict=config),
            subagent_definitions=resolve_subagent_definitions(ctx.config_dict),
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
            model_provider_currencies=public.model_provider_currencies,
            skills=await ctx.skills_registry.list_skills(),
            prompts_request=prompts_request,
            prompts=prompts,
        )

    @activity.defn(name="agent.state.prepare")
    async def prepare_state(self, call: NativeCall) -> PreparedState:
        return await self._recorded(call, PreparedState, self._prepare_state)

    async def _prepare_state(self, call, ctx):
        from gc_core.state_preparation import prepare_state

        request = value_to_type(StatePreparationRequest, call.data)
        return await prepare_state(
            workspace_config=ctx.config_dict,
            workspace=ctx.workspace,
            os_backend=ctx.os_backend,
            logger=ctx.logger,
            **asdict(request),
        )

    @activity.defn(name="agent.prompts.prepare")
    async def prepare_prompts(self, call: NativeCall) -> PreparedPrompts:
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
    async def prompt(self, call: NativeCall) -> str:
        return await self._recorded(call, str, self._prompt)

    async def _prompt(self, call, ctx):
        from gc_core.resource_loading import load_prompt_resource

        return await asyncio.to_thread(
            load_prompt_resource,
            value_to_type(PromptResourceRequest, call.data),
            ctx=ctx,
        )

    @activity.defn(name="agent.skills.list")
    async def skills(self, call: NativeCall) -> list[dict]:
        return await self._recorded(call, list[dict], self._skills)

    async def _skills(self, _call, ctx):
        return await ctx.skills_registry.list_skills()

    @activity.defn(name="agent.model.request")
    async def model(self, call: NativeCall) -> NativeModelReply:
        from gc_ua_temporalio.preview import commit_preview

        result = await self._recorded(call, NativeModelReply, self._model)
        await asyncio.to_thread(
            commit_preview, self.receipts, call.owner_id, call.operation_id
        )
        return result

    async def _model(self, call, ctx):
        from copy import copy

        from gc_ua_temporalio.model_error_encoding import encode_model_failure
        from gc_ua_temporalio.native_model_routing import (
            materialize_router_identity,
        )

        request = value_to_type(ModelRequest, call.data["request"])
        model_ctx = copy(ctx)
        from gc_ua_temporalio.preview import PreviewSink

        if call.profile in self.preview_profiles:
            sink = PreviewSink(
                self.receipts,
                call.owner_id,
                call.operation_id,
                attempt=activity.info().attempt
                if activity.in_activity()
                else 1,
            )
            model_ctx.on_stream_delta = sink.delta
        router = materialize_router_identity(call.data.get("routing"), ctx)
        model_ctx.model_router = router
        router.breaker.replace_from(call.data["circuit_breaker"])
        try:
            result = await LocalOperations(model_ctx).model(request)
        except ModelOperationError as exc:
            result = exc.result
            result.failure = encode_model_failure(exc.cause)
        return NativeModelReply(result, router.breaker.snapshot())

    @activity.defn(name="agent.tool.execute")
    async def tool(self, call: NativeCall) -> ToolResult:
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
            return (
                await host.execute(
                    owner,
                    request,
                    context_id=call.owner_id if call.child_context else None,
                )
            ).result
        return await execute_native_tool_effect(request, ctx=ctx)

    @activity.defn(name="agent.memory.execute")
    async def memory(self, call: NativeCall) -> ProjectMemoryReply:
        return await self._recorded(
            call, ProjectMemoryReply, self._memory, effect=True
        )

    async def _memory(self, call, ctx):
        from gc_core.tools.project.project_memory import ProjectMemory

        state = SimpleNamespace(project_memory_content_hash=None)
        return await asyncio.to_thread(
            ProjectMemory(ctx=ctx, state=state).execute_io,
            value_to_type(ProjectMemoryRequest, call.data),
        )

    @activity.defn(name="agent.memory.hash")
    async def memory_hash(self, call: NativeCall) -> str | None:
        return await self._recorded(call, str | None, self._memory_hash)

    async def _memory_hash(self, _call, ctx):
        from gc_core.project_memory import get_hash

        return await asyncio.to_thread(get_hash, ctx)

    @activity.defn(name="agent.guard.probe")
    async def guard(self, call: NativeCall) -> NativeGuardProbes:
        return await self._recorded(call, NativeGuardProbes, self._guard)

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
                evaluate_guard_probe(request, config_dict=ctx.config_dict)
                for request in requests
            ]

        primary, *companions = await asyncio.to_thread(evaluate)
        return NativeGuardProbes(primary, companions)

    @activity.defn(name="agent.summary.trace")
    async def summary_trace(self, call: NativeCall) -> bool:
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
    async def close(self, call: NativeCall) -> bool:
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
    async def release(self, call: NativeCall) -> bool:
        return await self._recorded(
            call, bool, self._release, resolve_context=False
        )

    async def _release(self, call, _ctx):
        release = getattr(self.context_provider, "release", None)
        if release is not None:
            await release(call)
        return True
