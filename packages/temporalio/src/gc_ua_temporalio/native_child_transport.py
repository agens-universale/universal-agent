"""Parent-side delivery and acknowledgement of the common child runner."""

import asyncio
from copy import deepcopy
from dataclasses import replace

from gc_core.context_preparation import PreparedRunContext
from gc_core.execution.contracts import (
    ExecutionFailure,
    InfrastructureFailure,
    OutcomeUnknown,
)
from gc_core.execution.human import HumanRequest, HumanRequestInterrupted
from gc_core.execution.resource_contracts import ResourceOwnerLost
from gc_core.tools.subagents.runner_transfer import (
    apply_subagent_transfer,
    capture_subagent_transfer,
)
from temporalio import workflow
from temporalio.converter import value_to_type
from temporalio.exceptions import ApplicationError, ChildWorkflowError

from gc_ua_temporalio.native_child_contracts import (
    NativeChildCheckpoint,
    NativeChildRequest,
    NativeChildResult,
)
from gc_ua_temporalio.native_operator_contracts import (
    NativePendingOperation,
    operation_digest,
)


class RemoteChildInput:
    def __init__(self, parent_call_id):
        self.parent_call_id = parent_call_id
        self.handle = None
        self.pending = []
        self.tasks = []

    def enqueue(self, message):
        self.pending.append(deepcopy(message))
        self.flush()

    def flush(self):
        if self.handle is not None:
            pending, self.pending = self.pending, []
            for message in pending:
                self.tasks.append(
                    asyncio.create_task(
                        self.handle.signal("submit_message", message)
                    )
                )

    async def drain_deliveries(self):
        if self.tasks:
            await asyncio.gather(*self.tasks)


class NativeChildTransport:
    def __init__(self, ctx, start, prepared, store, operations, human_request):
        self.ctx, self.start, self.prepared = ctx, start, prepared
        self.store, self.operations, self.human_request = (
            store,
            operations,
            human_request,
        )
        self.active_child = None
        self._handle = None
        self._state = None
        self._last = None
        self._sequence = 0

    def apply_checkpoint(self, payload):
        checkpoint = (
            value_to_type(NativeChildCheckpoint, payload)
            if isinstance(payload, dict)
            else payload
        )
        if checkpoint.child_id != self.active_child:
            return
        if checkpoint.sequence <= self._sequence:
            return
        apply_subagent_transfer(
            self._state, self.ctx, self._last, checkpoint.state
        )
        self._last, self._sequence = checkpoint.state, checkpoint.sequence

    async def answer_child_human(self, payload):
        if payload["child_id"] != self.active_child or self._handle is None:
            raise ExecutionFailure(
                "Human request belongs to an inactive child"
            )
        try:
            answer = await self.human_request(
                value_to_type(HumanRequest, payload["request"])
            )
        except HumanRequestInterrupted:
            return
        if self._state is None or self._state.stop_requested:
            return
        await self._handle.signal(
            "answer_human", args=[payload["request_id"], answer]
        )

    async def reconcile_child(self, payload, wait_pending):
        from gc_core.execution.contracts import OutcomeUnknown

        if payload["child_id"] != self.active_child or self._handle is None:
            return
        pending = value_to_type(NativePendingOperation, payload["pending"])
        if (
            pending.call.owner_id != self.active_child
            or pending.call.child_context is None
            or pending.call.child_context["parent_owner_id"]
            != self.operations.owner_id
            or pending.thread_owner != self.store.owner_id
            or pending.thread_epoch != self.store.owner_epoch
            or pending.generation != self.store._generation
            or (pending.store_id, pending.system_id, pending.thread_id)
            != (
                self.start.store_id,
                self.start.system_id,
                self.start.thread_id,
            )
        ):
            await self.stop_child()
            return
        try:
            result = await wait_pending(pending)
        except OutcomeUnknown:
            await self.stop_child()
            return
        if (
            self._handle is not None
            and self.active_child == payload["child_id"]
        ):
            await self._handle.signal(
                "resolve_operation",
                args=[
                    pending.call.operation_id,
                    operation_digest(pending.call),
                    result,
                ],
            )

    async def stop_child(self):
        if self._handle is not None:
            await self._handle.signal("stop")

    async def execute(self, runner, tool_name, arguments, *, parent_call_id):
        if self.active_child is not None:
            raise ExecutionFailure(
                "A child is already active for this context"
            )
        child_id = (
            f"{workflow.info().workflow_id}/{self.operations.new_id('child')}"
        )
        self._state = runner._orchestrator_state
        self._last, self._sequence = (
            capture_subagent_transfer(self._state, self.ctx),
            0,
        )
        config = {
            key: value
            for key, value in self.ctx.config_dict.items()
            if key
            not in ("_subagent_definitions_cache", "_subagent_diagnostics")
        }
        prepared = replace(
            self.prepared,
            context=PreparedRunContext.from_context(
                self.ctx, config_dict=config
            ),
            resource_owner=self.operations.resource_owner,
        )
        router = self.ctx.model_router
        child_context = {
            "parent_owner_id": self.operations.owner_id,
            "parent_context": self.operations.child_context,
            "tool_name": tool_name,
            "frozen_level": router.active_level(),
            "force_legacy": router.force_legacy,
            "circuit_breaker": router.breaker.snapshot(),
        }
        try:
            parent_snapshot = self._state.to_execution_snapshot()
        except ValueError as exc:
            raise ExecutionFailure(
                "Parent state cannot cross the child execution boundary"
            ) from exc
        request = NativeChildRequest(
            self.start,
            workflow.info().workflow_id,
            prepared,
            parent_snapshot,
            self._last,
            child_context,
            tool_name,
            arguments,
            parent_call_id,
            self.store.owner_id,
            self.store.owner_epoch,
            self.store._generation,
        )
        self.active_child = child_id
        scope = RemoteChildInput(parent_call_id)
        previous_scope = getattr(self.ctx, "child_input_scope", None)
        self.ctx.child_input_scope = scope
        try:
            self._handle = await workflow.start_child_workflow(
                "NativeSubagentWorkflow",
                request,
                id=child_id,
                result_type=NativeChildResult,
                cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            scope.handle = self._handle
            scope.flush()
            if self._state.stop_requested:
                await self.stop_child()
            try:
                result = await self._handle
            except ChildWorkflowError as exc:
                cause = exc.cause
                errors = {
                    error.__name__: error
                    for error in (
                        ExecutionFailure,
                        OutcomeUnknown,
                        InfrastructureFailure,
                        ResourceOwnerLost,
                    )
                }
                if (
                    isinstance(cause, ApplicationError)
                    and cause.type in errors
                ):
                    raise errors[cause.type](cause.message) from exc
                raise InfrastructureFailure(
                    "Native child workflow failed"
                ) from exc
            self.apply_checkpoint(result.checkpoint)
            return result.output
        finally:
            if getattr(self.ctx, "child_input_scope", None) is scope:
                self.ctx.child_input_scope = previous_scope
            try:
                await scope.drain_deliveries()
            finally:
                self.active_child = self._handle = self._state = self._last = (
                    None
                )


def install_native_subagents(
    ctx, start, prepared, store, operations, *, human_request
):
    transport = NativeChildTransport(
        ctx, start, prepared, store, operations, human_request
    )
    ctx.subagent_executor = transport.execute
    operations.stop_child = transport.stop_child
    return transport
