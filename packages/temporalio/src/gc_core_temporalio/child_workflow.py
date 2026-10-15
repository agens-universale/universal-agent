"""Native child transport executing the ordinary core SubagentRunner."""

import asyncio
import hashlib
from contextlib import contextmanager
from dataclasses import asdict

# Сторонние транспорты — из процесса воркера, до первого импорта ядра.
from gc_core_temporalio import sandbox_passthrough  # noqa: F401

# isort: split
from gc_core.execution.environment import execution_environment
from gc_core.execution.human import HumanRequestInterrupted
from gc_core.model_router import CircuitBreaker, ModelRouter
from gc_core.processing_responses import arun_agent
from gc_core.security.permission_store import PermissionStore
from gc_core.states import State
from gc_core.threads.models import Lease, ThreadKey
from gc_core.tools.subagents.active_scope import (
    activate_child_input,
    try_enqueue_user_message,
)
from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.tools.subagents.runner_transfer import capture_subagent_transfer
from gc_core.tools.subagents.sidecar import bind_thread_identity
from temporalio import workflow

from gc_core_temporalio.child_contracts import (
    ChildCheckpoint,
    ChildRequest,
    ChildResult,
    PreparedChild,
)
from gc_core_temporalio.child_preparation import PreparedChildContext
from gc_core_temporalio.context import assemble_context
from gc_core_temporalio.operations import (
    WorkflowOperations,
    session_failure,
)
from gc_core_temporalio.thread_store_proxy import TemporalThreadStore
from gc_core_temporalio.thread_store_transport import ThreadStoreTransport


async def drain_signals(tasks, *, failed):
    """Дождаться отправленных родителю сигналов (checkpoint, вопрос).

    Ребёнок уже упал — сбой сигнала (родитель закрыт) не подменяет его
    ошибку: родителю важна причина отказа, а не то, что сигнал не дошёл.
    Ход ребёнка удался — сбой сигнала поднимается, как раньше.
    """
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=failed)


@workflow.defn(name="UniversalSubagentWorkflow")
class UniversalSubagentWorkflow:
    def __init__(self):
        self.sequence = 0
        self.checkpoint_sequence = 0
        self.parent_state = None
        self.parent_ctx = None
        self.pending_messages = []
        self.stopped = False
        self.replies = {}
        self.refused = set()
        #: Человек отвечает (``human_input`` родителя); до старта — да.
        self.human_input = True
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

    @workflow.signal
    def refuse_human(self, request_id: str):
        # Родитель не дал ответа (человека нет, вопрос снят): отказ, а не
        # вечное ожидание.
        self.refused.add(request_id)

    async def human_request(self, request):
        if not self.human_input:
            # Без человека (``human_input: false``) — отказ сразу, как у
            # родителя: спрашивать его некого.
            raise HumanRequestInterrupted(
                "No human answers in this deployment (human_input: false)"
            )
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
            lambda: (
                self.stopped
                or request_id in self.replies
                or request_id in self.refused
            )
        )
        if self.stopped or request_id not in self.replies:
            raise HumanRequestInterrupted("Child human request interrupted")
        return self.replies[request_id]

    def checkpoint(self, state, ctx):
        self.checkpoint_sequence += 1
        checkpoint = ChildCheckpoint(
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
            try:
                yield scope
            finally:
                # Пришедшее после последней выборки цикла не теряется:
                # вернётся родителю как undelivered.
                self.pending_messages.extend(scope.drain())

    @workflow.run
    async def run(self, request: ChildRequest) -> ChildResult:
        self.parent = workflow.get_external_workflow_handle(
            request.parent_workflow_id
        )
        with execution_environment(new_id=self.new_id, now=workflow.now):
            operations = WorkflowOperations(
                request.start, workflow.info().workflow_id, self.new_id
            )
            operations.child_context = request.child_context
            self.human_input = request.parent_context.human_input
            operations.resource_owner = request.parent_context.resource_owner
            operations.tool_metadata = request.parent_context.tool_metadata
            if request.parent_context.activity_settings:
                operations.activity_settings = (
                    request.parent_context.activity_settings
                )
            try:
                prepared = await operations.call(
                    "universal_agent.subagent.prepare",
                    {
                        "store_id": request.start.store_id,
                        "parent_state": request.parent_state,
                    },
                    PreparedChild,
                )
                store = TemporalThreadStore(
                    request.start.store_id,
                    ThreadStoreTransport(
                        task_queue=request.start.resource_queue,
                        settings=operations.activity_settings["thread_store"],
                    ),
                    owner=request.store_owner_id or request.parent_workflow_id,
                )
                if request.store_owner_epoch is not None:
                    store.adopt(
                        ThreadKey(
                            system=request.start.system_id,
                            thread=request.start.thread_id,
                        ),
                        Lease(
                            owner=request.store_owner_id,
                            epoch=request.store_owner_epoch,
                        ),
                    )
                operations.thread_store = store
                parent_ctx = assemble_context(
                    request.parent_context,
                    operations=operations,
                    store=store,
                    logger=workflow.logger,
                    human_request=self.human_request,
                )
                parent_ctx.model_router = ModelRouter(
                    parent_ctx,
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
                child_ctx = assemble_context(
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
                    preparation=PreparedChildContext(prepared, child_ctx),
                    input_scope_factory=self.input_scope,
                )
                failed = True
                try:
                    output = await runner.arun(
                        request.tool_name,
                        request.arguments,
                        arun_agent,
                        parent_call_id=request.parent_call_id,
                    )
                    failed = False
                finally:
                    # Цикл ребёнка закончился: дальнейший ввод — родителю.
                    self.parent_ctx = None
                    checkpoint = self.checkpoint(parent_state, parent_ctx)
                    await drain_signals(self.signal_tasks, failed=failed)
                undelivered, self.pending_messages = self.pending_messages, []
            except asyncio.CancelledError:
                await operations.release_context()
                raise
            except Exception as exc:
                # Как у родителя: доменный отказ — FAILED ребёнка; сбой кода
                # workflow — только workflow task, без новых команд.
                failure = session_failure(exc)
                if failure is None:
                    raise
                await operations.release_context()
                if failure is exc:
                    raise
                raise failure from exc
            await operations.release_context()
            return ChildResult(output, checkpoint, undelivered)
