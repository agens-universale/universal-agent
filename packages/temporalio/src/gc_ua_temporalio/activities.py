"""Activity boundaries. Completed results are cached by logical operation ID."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from dataclasses import asdict

import httpx
from gc_core.execution.arguments_error import WrongFunctionArgumentsException
from gc_core.execution.contracts import (
    ModelOperationCancelled,
    ModelOperationError,
    ModelResult,
    ProjectionAck,
    ProjectionBatch,
    ToolResult,
)
from temporalio import activity
from temporalio.converter import value_to_type
from temporalio.exceptions import ApplicationError

from gc_ua_temporalio.contracts import (
    BootstrapRequest,
    BootstrapSnapshot,
    ModelActivityInput,
    ToolActivityInput,
)
from gc_ua_temporalio.control_contracts import (
    EffectResult,
    ExecuteEffectInput,
    PreparedTool,
    PrepareToolInput,
)
from gc_ua_temporalio.guarded_tools import GuardedTools
from gc_ua_temporalio.runtime import WorkerRuntime, payload_digest
from gc_ua_temporalio.storage.sqlite import ProjectionConflict
from gc_ua_temporalio.wire import validate_payload


def classified_error(exc):
    if isinstance(exc, ApplicationError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return ApplicationError(
            f"Model HTTP {code}",
            type="ModelTransportError",
            non_retryable=not (code == 429 or code >= 500),
        )
    if isinstance(exc, (httpx.TransportError, sqlite3.OperationalError)):
        return ApplicationError(
            type(exc).__name__, type="InfrastructureFailure"
        )
    if isinstance(exc, (ValueError, KeyError, ProjectionConflict)):
        return ApplicationError(
            str(exc), type="InvalidExecutionRequest", non_retryable=True
        )
    return ApplicationError(
        type(exc).__name__, type="ExecutionFailure", non_retryable=True
    )


class AgentActivities:
    def __init__(self, runtime: WorkerRuntime):
        self.runtime = runtime
        self.guarded = GuardedTools(runtime)

    async def _recorded(self, data, operation, result_type, call):
        return await self._heartbeat_call(
            self._recorded_attempt, data, operation, result_type, call
        )

    async def _heartbeat_call(self, call, *args):
        task = asyncio.create_task(self._heartbeat())
        try:
            return await call(*args)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    @staticmethod
    async def _heartbeat():
        while True:
            activity.heartbeat()
            await asyncio.sleep(2)

    async def _recorded_attempt(self, data, operation, result_type, call):
        if data.schema_version != 1:
            raise ApplicationError(
                "Unsupported wire version",
                type="InvalidExecutionRequest",
                non_retryable=True,
            )
        digest = payload_digest(data)
        cached = self.runtime.store.operation_result(
            data.owner, operation, digest
        )
        if cached is not None:
            return value_to_type(result_type, cached)
        try:
            result = await call(data)
            validate_payload(result)
            stored = self.runtime.store.save_operation_result(
                data.owner, operation, digest, asdict(result)
            )
            return value_to_type(result_type, stored)
        except Exception as exc:
            raise classified_error(exc) from None

    @activity.defn(name="ua.bootstrap")
    async def bootstrap(self, data: BootstrapRequest) -> BootstrapSnapshot:
        return await self._recorded(
            data, "bootstrap", BootstrapSnapshot, self.runtime.bootstrap
        )

    @activity.defn(name="ua.model")
    async def model(self, data: ModelActivityInput) -> ModelResult:
        result = await self._recorded(
            data, data.request.operation_id, ModelResult, self._model
        )
        from gc_ua_temporalio.storage.views import SessionViews

        SessionViews(self.runtime.store).emit(
            data.owner,
            data.request.operation_id,
            activity.info().attempt,
            1000,
            "committed",
            {},
        )
        return result

    async def _model(self, data):
        from gc_core.execution.finish_reason import is_context_overflow_error

        from gc_ua_temporalio.model_error_encoding import encode_model_failure

        try:
            return await self.runtime.model(data)
        except ModelOperationCancelled as exc:
            raise exc.cause from None
        except ModelOperationError as exc:
            # Native transport/adaptive/auth retries have already finished.
            # Record their outcome and usage together; retrying this activity
            # would repeat the entire native policy and lose prior billing.
            exc.result.failure = encode_model_failure(exc.cause)
            return exc.result
        except Exception as exc:
            if is_context_overflow_error(exc):
                return ModelResult(context_overflow=True)
            raise

    @activity.defn(name="ua.tool")
    async def tool(self, data: ToolActivityInput) -> ToolResult:
        return await self._recorded(
            data, data.invocation.operation_id, ToolResult, self._tool
        )

    async def _tool(self, data):
        try:
            return await self.runtime.tool(data)
        except WrongFunctionArgumentsException as exc:
            return self._invalid_arguments(exc, data)

    @staticmethod
    def _invalid_arguments(exc, data):
        from types import SimpleNamespace

        from gc_core.processing_responses import _attach_truncation_hint

        exc.call_id = data.invocation.call_id
        _attach_truncation_hint(
            exc,
            SimpleNamespace(last_finish_reason=data.invocation.finish_reason),
        )
        return ToolResult(
            output=exc.to_tool_output(),
            outcome="tool_error",
            invalid_arguments=True,
        )

    @activity.defn(name="ua.prepare_tool")
    async def prepare(self, data: PrepareToolInput) -> PreparedTool:
        return await self._recorded(
            data,
            f"prepare:{data.tool.invocation.operation_id}:{data.sequence}",
            PreparedTool,
            self._prepare,
        )

    async def _prepare(self, data):
        try:
            return await self.guarded.prepare(data)
        except WrongFunctionArgumentsException as exc:
            return PreparedTool(
                "", result=self._invalid_arguments(exc, data.tool)
            )

    @activity.defn(name="ua.effect")
    async def effect(self, data: ExecuteEffectInput) -> EffectResult:
        return await self._guarded_call(self.guarded.execute, data)

    @activity.defn(name="ua.reconcile")
    async def reconcile(self, data: ExecuteEffectInput) -> EffectResult:
        return await self._guarded_call(self.guarded.reconcile, data)

    async def _guarded_call(self, call, data):
        try:
            result = await self._heartbeat_call(call, data)
            validate_payload(result)
            return result
        except Exception as exc:
            raise classified_error(exc) from None

    @activity.defn(name="ua.persist")
    async def persist(self, batch: ProjectionBatch) -> ProjectionAck:
        try:
            return self.runtime.store.commit_batch(batch)
        except Exception as exc:
            raise classified_error(exc) from None
