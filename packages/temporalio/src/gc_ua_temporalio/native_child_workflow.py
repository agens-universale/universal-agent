"""Native child transport executing the ordinary core SubagentRunner."""

import asyncio
import hashlib
from contextlib import contextmanager
from dataclasses import asdict

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import pydantic  # noqa: F401

from gc_core.execution.environment import execution_environment
from gc_core.execution.human import HumanRequestInterrupted
from gc_core.model_router import CircuitBreaker, ModelRouter
from gc_core.processing_responses import arun_agent
from gc_core.security.permission_store import PermissionStore
from gc_core.states import State
from gc_core.tools.subagents.active_scope import (
    activate_child_input,
    try_enqueue_user_message,
)
from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.tools.subagents.runner_transfer import capture_subagent_transfer
from gc_core.tools.subagents.sidecar import bind_thread_identity

from gc_ua_temporalio.native_child_contracts import (
    NativeChildCheckpoint,
    NativeChildRequest,
    NativeChildResult,
    NativePreparedChild,
)
from gc_ua_temporalio.native_child_preparation import PreparedNativeChild
from gc_ua_temporalio.native_context import assemble_native_context
from gc_ua_temporalio.native_operations import NativeOperations
from gc_ua_temporalio.thread_store_proxy import TemporalThreadStore
from gc_ua_temporalio.thread_store_transport import ThreadStoreTransport


@workflow.defn(
    name="NativeSubagentWorkflow",
    failure_exception_types=[Exception],
)
class NativeSubagentWorkflow:
    def __init__(self):
        self.sequence = 0
        self.checkpoint_sequence = 0
        self.parent_state = None
        self.parent_ctx = None
        self.pending_messages = []
        self.stopped = False
        self.replies = {}
        self.signal_tasks = []

    def new_id(self, prefix):
        self.sequence += 1
        info = workflow.info()
        digest = hashlib.sha256(
            f"{info.workflow_id}/{info.run_id}/{prefix}/{self.sequence}".encode()
        ).hexdigest()
        return f"{prefix}_{digest}"

    @workflow.signal
    def stop(self):
        self.stopped = True
        if self.parent_state is not None:
            self.parent_state.stop_requested = True

    @workflow.signal
    def submit_message(self, message: dict):
        # Signal handlers run in separate tasks and do not inherit run()'s
        # contextvars. Sidecar tasks created by enqueue need the same sources.
        with execution_environment(new_id=self.new_id, now=workflow.now):
            if self.parent_ctx is None or not try_enqueue_user_message(
                self.parent_ctx, message
            ):
                self.pending_messages.append(message)

    @workflow.signal
    def answer_human(self, request_id: str, answer: str):
        self.replies.setdefault(request_id, answer)

    async def human_request(self, request):
        request_id = self.new_id("human")
        await self.parent.signal(
            "child_human",
            {
                "child_id": workflow.info().workflow_id,
                "request_id": request_id,
                "request": asdict(request),
            },
        )
        await workflow.wait_condition(
            lambda: self.stopped or request_id in self.replies
        )
        if self.stopped:
            raise HumanRequestInterrupted("Child human request interrupted")
        return self.replies[request_id]

    def checkpoint(self, state, ctx):
        self.checkpoint_sequence += 1
        checkpoint = NativeChildCheckpoint(
            workflow.info().workflow_id,
            self.checkpoint_sequence,
            capture_subagent_transfer(state, ctx),
        )
        self.signal_tasks.append(
            asyncio.create_task(
                self.parent.signal("child_checkpoint", asdict(checkpoint))
            )
        )
        return checkpoint

    @contextmanager
    def input_scope(self, ctx, *, parent_call_id):
        with activate_child_input(
            ctx, parent_call_id=parent_call_id, thread_safe=False
        ) as scope:
            original = scope.set_on_enqueue

            def set_callback(callback):
                original(callback)
                if callback is not None:
                    pending, self.pending_messages = self.pending_messages, []
                    for message in pending:
                        scope.enqueue(message)

            scope.set_on_enqueue = set_callback
            yield scope

    @workflow.run
    async def run(self, request: NativeChildRequest) -> NativeChildResult:
        self.parent = workflow.get_external_workflow_handle(
            request.parent_workflow_id
        )
        with execution_environment(new_id=self.new_id, now=workflow.now):
            operations = NativeOperations(
                request.start, workflow.info().workflow_id, self.new_id
            )
            operations.child_context = request.child_context
            operations.resource_owner = request.parent_context.resource_owner
            operations.tool_metadata = request.parent_context.tool_metadata
            try:
                prepared = await operations.call(
                    "agent.subagent.prepare",
                    {
                        "store_id": request.start.store_id,
                        "parent_state": request.parent_state,
                    },
                    NativePreparedChild,
                )
                store = TemporalThreadStore(
                    request.start.store_id,
                    ThreadStoreTransport(
                        task_queue=request.start.resource_queue
                    ),
                    owner_id=request.store_owner_id,
                    owner_epoch=request.store_owner_epoch,
                    generation=request.store_generation,
                    capabilities=prepared.context.store_capabilities,
                    threads_dir=prepared.context.threads_dir,
                )
                parent_ctx = assemble_native_context(
                    request.parent_context,
                    operations=operations,
                    store=store,
                    logger=workflow.logger,
                    human_request=self.human_request,
                )
                parent_ctx.model_router = ModelRouter(
                    parent_ctx.config_dict,
                    level_provider=lambda: request.child_context[
                        "frozen_level"
                    ],
                    force_legacy=request.child_context["force_legacy"],
                    breaker=CircuitBreaker.restore(
                        request.parent_transfer.circuit_breaker
                    ),
                )
                parent_state = State.from_execution_snapshot(
                    request.parent_state
                )
                parent_state.workspace, parent_state.logger = (
                    parent_ctx.workspace,
                    workflow.logger,
                )
                parent_state.stop_requested = (
                    parent_state.stop_requested or self.stopped
                )
                parent_ctx.permission_store = PermissionStore(
                    parent_state.shared.confirm_rules
                )
                parent_ctx.permission_store.replace_run_rules(
                    request.parent_transfer.run_permissions
                )
                bind_thread_identity(
                    parent_ctx,
                    threads_dir=store.threads_dir,
                    system_id=request.start.system_id,
                    thread_id=request.start.thread_id,
                )
                child_ctx = assemble_native_context(
                    prepared.context,
                    operations=operations,
                    store=store,
                    logger=workflow.logger,
                    human_request=self.human_request,
                )
                parent_ctx.subagent_checkpoint = self.checkpoint
                self.parent_ctx, self.parent_state = parent_ctx, parent_state
                runner = SubagentRunner(
                    parent_ctx,
                    parent_state,
                    parent_ctx.config_dict,
                    preparation=PreparedNativeChild(prepared, child_ctx),
                    input_scope_factory=self.input_scope,
                )
                try:
                    output = await runner.arun(
                        request.tool_name,
                        request.arguments,
                        arun_agent,
                        parent_call_id=request.parent_call_id,
                    )
                finally:
                    # Цикл ребёнка закончился: дальнейший ввод — родителю.
                    self.parent_ctx = None
                    checkpoint = self.checkpoint(parent_state, parent_ctx)
                    if self.signal_tasks:
                        await asyncio.gather(*self.signal_tasks)
                undelivered, self.pending_messages = self.pending_messages, []
                return NativeChildResult(output, checkpoint, undelivered)
            finally:
                await operations.release_context()
