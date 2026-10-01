"""Temporal delivery and waiting around the ordinary AgentExecutor lifecycle."""

import asyncio
import hashlib
from dataclasses import asdict, replace

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    import pydantic  # noqa: F401

from gc_core.agent_executor import AgentExecutor
from gc_core.execution.contracts import ExecutionFailure, ModelProviderError
from gc_core.execution.environment import execution_environment
from gc_core.execution.human import HumanRequestInterrupted
from gc_core.security.confirm_port import (
    CONFIRM_ALLOW_ONCE,
    CONFIRM_ALLOW_REMEMBER,
    CONFIRM_DENY_ONCE,
    CONFIRM_DENY_REMEMBER,
)
from gc_core.structured_output import (
    StructuredOutputError,
    arephrase_turn_outcome,
)
from gc_core.thread_store_durable import ThreadStoreOperation

from gc_ua_temporalio.native_checkpoint_transport import (
    INLINE_BYTES,
    encode_resume,
    load_checkpoint,
    save_checkpoint,
)
from gc_ua_temporalio.native_child_transport import install_native_subagents
from gc_ua_temporalio.native_context import assemble_native_context
from gc_ua_temporalio.native_contracts import (
    NativePreparedContext,
    NativeResume,
    NativeRunRequest,
)
from gc_ua_temporalio.native_operations import NativeOperations
from gc_ua_temporalio.native_operator_contracts import NativeOperatorResolution
from gc_ua_temporalio.session_contracts import UserMessage
from gc_ua_temporalio.thread_store_proxy import TemporalThreadStore
from gc_ua_temporalio.thread_store_transport import ThreadStoreTransport


def _message_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@workflow.defn(
    name="NativeAgentWorkflow",
    failure_exception_types=[ExecutionFailure, ModelProviderError, ValueError],
)
class NativeAgentWorkflow:
    def __init__(self):
        self.start = None
        self.active_turn_id = None
        self.executor = None
        self.children = None
        self.operations = None
        self.phase = "starting"
        self.sequence = 0
        self.run_identity = ""
        self.pending_operation = None
        self.reconciliation_result = None
        self.reconciliation_receipts = {}
        self.pending_human = None
        self.human_replies = {}
        self.reply_receipts = {}
        self.stop_requested = False
        self.pending_messages = []
        self.undelivered = []
        self.accepted = {}
        self.accepting_inputs = False
        self.closed = False
        self.ready = False
        self.delivery_epoch = 0
        self.turns = 0
        self.answer = None
        self.formatted_answer = None
        self.post_error = None
        self.terminal_outcome = None

    def new_id(self, prefix):
        self.sequence += 1
        digest = hashlib.sha256(
            f"{self.run_identity}/{prefix}/{self.sequence}".encode()
        ).hexdigest()
        return f"{prefix}_{digest}"

    def _delivery_changed(self):
        self.delivery_epoch += 1

    @workflow.query
    def status(self) -> dict:
        state = self.executor.get_state() if self.executor else None
        return {
            "phase": self.phase,
            "turn_id": self.active_turn_id,
            "system_id": self.start.system_id if self.start else None,
            "thread_id": self.start.thread_id if self.start else None,
            "run_id": self.run_identity,
            "owner": self.operations.owner_id if self.operations else None,
            "active_child": self.children.active_child
            if self.children
            else None,
            "pending_human": self.pending_human,
            "pending_operation": self.pending_operation.public()
            if self.pending_operation
            else None,
            "child": {
                "workflow_id": self.children.active_child,
                "pending_human": self.pending_human,
            }
            if self.children and self.children.active_child
            else None,
            "stop_requested": self.stop_requested,
            "steps": state.steps if state else 0,
            "tool_calls": state.tool_calls if state else 0,
            "turns": self.turns,
            "answer": self.answer,
            "formatted_answer": self.formatted_answer,
            "post_error": self.post_error,
            "mailbox_size": len(self.pending_messages),
            "undelivered": self.undelivered,
        }

    async def operator_activity(self, name, request):
        from datetime import timedelta

        from temporalio.common import RetryPolicy

        return await workflow.execute_activity(
            name,
            request,
            result_type=dict,
            task_queue=self.start.resource_queue,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    async def wait_reconciliation(self, name, call, store):
        from gc_ua_temporalio.native_operator_contracts import (
            NativePendingOperation,
        )

        pending = NativePendingOperation(
            name,
            call,
            self.start.store_id,
            self.start.system_id,
            self.start.thread_id,
            store.owner_id,
            store.owner_epoch,
            store._generation,
        )
        return await self.wait_pending_operation(pending)

    async def wait_pending_operation(self, pending):
        from gc_core.execution.contracts import OutcomeUnknown

        if self.pending_operation is not None:
            raise OutcomeUnknown(
                "Another operation already requires reconciliation"
            )
        previous_phase = self.phase
        self.pending_operation = pending
        self.reconciliation_result = None
        self.phase = "needs_reconciliation"
        try:
            await workflow.wait_condition(
                lambda: (
                    self.reconciliation_result is not None
                    or self.stop_requested
                    or self.closed
                )
            )
            if self.stop_requested or self.closed:
                raise OutcomeUnknown(
                    "Uncertain operation aborted; its ledger remains unresolved"
                )
            return self.reconciliation_result
        finally:
            self.pending_operation = None
            self.reconciliation_result = None
            self.phase = previous_phase

    @workflow.update
    async def inspect_operation(self) -> dict:
        await workflow.wait_condition(lambda: self.ready)
        from gc_ua_temporalio.native_operator_contracts import (
            NativeOperatorCall,
        )

        if self.pending_operation is None:
            raise ApplicationError(
                "No suspended operation", non_retryable=True
            )
        return await self.operator_activity(
            "agent.operator.inspect",
            NativeOperatorCall(self.pending_operation),
        )

    @workflow.update
    async def resume_operation(self, operation_id: str, digest: str) -> dict:
        await workflow.wait_condition(lambda: self.ready)
        from gc_ua_temporalio.native_operator_contracts import (
            NativeOperatorCall,
            operation_digest,
        )

        pending = self.pending_operation
        if pending is None or self.stop_requested or self.closed:
            raise ApplicationError(
                "No active suspended operation", non_retryable=True
            )
        if (
            operation_id != pending.call.operation_id
            or digest != operation_digest(pending.call)
        ):
            raise ApplicationError(
                "Stale operation identity", non_retryable=True
            )
        result = await self.operator_activity(
            "agent.operator.result", NativeOperatorCall(pending)
        )
        if (
            self.pending_operation is pending
            and not self.stop_requested
            and not self.closed
        ):
            self.reconciliation_result = result
        return {"operation_id": operation_id, "resumed": True}

    @workflow.update
    async def reconcile_operation(
        self, resolution: NativeOperatorResolution
    ) -> dict:
        await workflow.wait_condition(lambda: self.ready)
        from dataclasses import asdict

        from gc_ua_temporalio.native_operator_contracts import (
            NativeOperatorCall,
        )

        previous = self.reconciliation_receipts.get(resolution.request_id)
        if previous is not None:
            if previous[0] != asdict(resolution):
                raise ApplicationError(
                    "Resolution request ID reused", non_retryable=True
                )
            return {**previous[1], "duplicate": True}
        if (
            self.pending_operation is None
            or self.stop_requested
            or self.closed
        ):
            raise ApplicationError(
                "No active suspended operation", non_retryable=True
            )
        pending = self.pending_operation
        receipt = await self.operator_activity(
            "agent.operator.resolve", NativeOperatorCall(pending, resolution)
        )
        result = await self.operator_activity(
            "agent.operator.result", NativeOperatorCall(pending)
        )
        self.reconciliation_receipts[resolution.request_id] = [
            asdict(resolution),
            receipt,
        ]
        self._delivery_changed()
        if (
            self.pending_operation is pending
            and not self.stop_requested
            and not self.closed
        ):
            self.reconciliation_result = result
        return receipt

    @workflow.signal
    async def child_reconciliation(self, payload: dict):
        if self.children is not None:
            await self.children.reconcile_child(
                payload, self.wait_pending_operation
            )

    @workflow.signal
    async def child_human(self, payload: dict):
        if self.children is not None:
            await self.children.answer_child_human(payload)

    @workflow.signal
    def child_checkpoint(self, payload: dict):
        if self.children is not None:
            self.children.apply_checkpoint(payload)

    @workflow.signal
    async def stop(self):
        if self.phase in {"idle", "stopped"} and not self.accepting_inputs:
            return
        self.stop_requested = True
        self._delivery_changed()
        if self.executor is not None:
            self.executor.stop_run()
        stop_child = getattr(self.operations, "stop_child", None)
        if stop_child is not None:
            await stop_child()

    @workflow.update
    async def stop_turn(self, turn_id: str) -> dict:
        await workflow.wait_condition(lambda: self.ready)
        if turn_id != self.active_turn_id:
            raise ApplicationError("Turn is stale", non_retryable=True)
        await self.stop()
        return {"turn_id": turn_id, "stop_requested": self.stop_requested}

    @workflow.signal
    def close_session(self):
        """Finish accepted work, then close; this does not abort a tool."""
        self.closed = True
        self._delivery_changed()

    @workflow.update
    async def submit_message(self, message: UserMessage) -> dict:
        message.validate()
        await workflow.wait_condition(lambda: self.ready)
        digest = _message_digest(message.text)
        existing = self.accepted.get(message.message_id)
        if existing is not None:
            if existing != digest:
                raise ApplicationError("Message ID reused", non_retryable=True)
            return {"message_id": message.message_id, "duplicate": True}
        if self.closed:
            raise ApplicationError("Session is closing", non_retryable=True)
        self.accepted[message.message_id] = digest
        self._delivery_changed()
        value = {
            "role": "user",
            "content": message.text,
            "message_id": message.message_id,
        }
        if self.accepting_inputs:
            self.executor.add_message(value)
        else:
            self.pending_messages.append(value)
        return {"message_id": message.message_id, "duplicate": False}

    @workflow.update
    async def answer_human(
        self, request_id: str, answer: str, reply_id: str | None = None
    ) -> bool:
        await workflow.wait_condition(lambda: self.ready)
        if reply_id in self.reply_receipts:
            if self.reply_receipts[reply_id] != [request_id, answer]:
                raise ApplicationError("Reply ID reused", non_retryable=True)
        else:
            self._answer_human(request_id, answer)
            if reply_id is not None:
                self.reply_receipts[reply_id] = [request_id, answer]
                self._delivery_changed()
        return True

    def _answer_human(self, request_id, answer) -> None:
        existing = self.human_replies.get(request_id)
        if existing is not None:
            if existing != answer:
                raise ApplicationError(
                    "Human reply changed", non_retryable=True
                )
            return
        if (
            not self.pending_human
            or self.pending_human["request_id"] != request_id
            or self.stop_requested
        ):
            raise ApplicationError(
                "Human request is stale", non_retryable=True
            )
        if self.pending_human["kind"] == "confirm" and answer not in {
            CONFIRM_ALLOW_ONCE,
            CONFIRM_ALLOW_REMEMBER,
            CONFIRM_DENY_ONCE,
            CONFIRM_DENY_REMEMBER,
        }:
            raise ApplicationError(
                "Unknown confirmation choice", non_retryable=True
            )
        self.human_replies[request_id] = answer
        self._delivery_changed()

    async def human_request(self, request):
        request_id = self.new_id("human")
        self.pending_human = {**asdict(request), "request_id": request_id}
        self.phase = "waiting_human"
        try:
            await workflow.wait_condition(
                lambda: self.stop_requested or request_id in self.human_replies
            )
            if self.stop_requested:
                raise HumanRequestInterrupted(
                    "Agent was stopped while waiting for a human"
                )
            return self.human_replies[request_id]
        finally:
            self.pending_human = None
            self.phase = "running"

    def _initialize(self, start):
        self.start = start
        if start.format_text is not None and not start.format_text.strip():
            raise ValueError("Structured output format must be nonempty")
        if start.schema_version != 1 or start.continue_after_turns < 1:
            raise ValueError("Invalid native run request")
        if start.resume is not None:
            self._restore_delivery(start.resume)
        else:
            self._initial_messages(start)
        self.phase = "idle"
        self.ready = True

    def _restore_delivery(self, resume):
        if resume.schema_version != 1:
            raise ValueError("Unsupported native resume")
        self.closed = self.closed or resume.closed
        self.stop_requested = self.stop_requested or resume.stop_requested
        self.human_replies = dict(resume.human_replies)
        self.reply_receipts = dict(resume.reply_receipts)
        self.reconciliation_receipts = dict(resume.reconciliation_receipts)
        self.undelivered = list(resume.undelivered)
        self.accepted = dict(resume.accepted)
        self.pending_messages = list(resume.pending)
        self.turns = resume.turns
        self.answer = resume.answer
        self.formatted_answer = resume.formatted_answer
        self.post_error = resume.post_error
        self.terminal_outcome = resume.terminal_outcome

    def _initial_messages(self, start):
        if not start.messages:
            raise ValueError("Native run requires messages")
        ids = start.message_ids or [self.new_id("m") for _ in start.messages]
        if len(ids) != len(start.messages) or len(set(ids)) != len(ids):
            raise ValueError("Message IDs must match messages and be unique")
        for message_id, text in zip(ids, start.messages):
            message = UserMessage(message_id, text)
            message.validate()
            self.accepted[message_id] = _message_digest(text)
            self.pending_messages.append(
                {"role": "user", "content": text, "message_id": message_id}
            )

    async def _prepare_turn(self, start, owner, store):
        self.phase = "starting"
        self.active_turn_id = (
            f"{workflow.info().workflow_id}/turn-{self.turns + 1}"
        )
        self.operations = NativeOperations(
            start, f"{owner}/turn-{self.turns + 1}", self.new_id
        )
        prepared = await self.operations.call(
            "agent.context.prepare",
            {"store_id": start.store_id},
            NativePreparedContext,
        )
        self.operations.resource_owner = prepared.resource_owner
        if store is None:
            store = TemporalThreadStore(
                start.store_id,
                ThreadStoreTransport(task_queue=start.resource_queue),
                capabilities=prepared.store_capabilities,
                threads_dir=prepared.threads_dir,
            )
            snapshot = await store.aread_snapshot(
                start.system_id, start.thread_id, include_state=False
            )
            receipt = await store.acommit_operation(
                start.system_id,
                start.thread_id,
                ThreadStoreOperation(
                    self.new_id("claim"),
                    "claim_owner",
                    {"owner_id": owner},
                    expected_revision=snapshot.revision,
                    generation=snapshot.generation,
                ),
            )
            store.owner_id, store.owner_epoch = owner, receipt.owner_epoch
        ctx = assemble_native_context(
            prepared,
            operations=self.operations,
            store=store,
            logger=workflow.logger,
            human_request=self.human_request,
        )
        self.children = install_native_subagents(
            ctx,
            start,
            prepared,
            store,
            self.operations,
            human_request=self.human_request,
        )
        self.operations.reconciliation_wait = lambda name, call: (
            self.wait_reconciliation(name, call, store)
        )
        self.ctx = ctx
        self.executor = AgentExecutor(ctx)
        return store

    async def _run_turn(self, start):
        messages, self.pending_messages = self.pending_messages, []
        if self.stop_requested:
            self.executor.stop_run()
        self.phase = "running"
        self.accepting_inputs = True
        ran = False
        self.formatted_answer = self.post_error = None
        try:
            self.answer = await self.executor.arun(
                [value["content"] for value in messages],
                thread_id=start.thread_id,
                system=start.system_id,
                message_ids=[value["message_id"] for value in messages],
                account_run=start.format_text is None,
            )
            ran = True
            if start.format_text is not None:
                self.phase = "postprocessing"
                try:
                    self.formatted_answer = await arephrase_turn_outcome(
                        format_text=start.format_text,
                        response=self.answer,
                        ctx=self.ctx,
                        state=self.executor.get_state(),
                        terminal_outcome=self.executor.last_terminal_outcome,
                    )
                except StructuredOutputError as exc:
                    self.post_error = str(exc)
        finally:
            try:
                if ran and start.format_text is not None:
                    self.phase = "accounting"
                    await self.executor.aclose_run_accounting()
            finally:
                self.accepting_inputs = False
                self.pending_messages.extend(
                    self.executor.take_undelivered_messages()
                )
        if self.stop_requested:
            self.undelivered.extend(self.pending_messages)
            self.pending_messages = []
        await self.operations.release_context()
        self.turns += 1
        self.terminal_outcome = self.executor.last_terminal_outcome
        self.phase = "stopped" if self.stop_requested else "idle"

    @workflow.run
    async def run(self, start: NativeRunRequest) -> dict:
        self.run_identity = workflow.info().run_id
        self.start = start
        if start.resume_ref is not None:
            if start.resume is not None:
                raise ValueError("Native resume must be inline or referenced")
            self.phase = "restoring_delivery"
            start = replace(
                start, resume=await load_checkpoint(start), resume_ref=None
            )
        self._initialize(start)
        with execution_environment(new_id=self.new_id, now=workflow.now):
            try:
                result = await self._run_session(start)
                cancellation = workflow.cancellation_reason()
                if cancellation is not None:
                    raise asyncio.CancelledError(cancellation)
                return result
            except (Exception, asyncio.CancelledError):
                self.closed = True
                self.accepting_inputs = False
                self.phase = "failed"
                if self.operations is not None:
                    try:
                        await asyncio.shield(self.operations.mcp_cleanup())
                        await asyncio.shield(self.operations.release_context())
                    except (Exception, asyncio.CancelledError):
                        workflow.logger.exception(
                            "Native context cleanup failed"
                        )
                raise

    async def _run_session(self, start):
        owner = f"{workflow.info().workflow_id}/{workflow.info().run_id}"
        store = None
        started_turns = self.turns
        while True:
            if self.pending_messages:
                store = await self._prepare_turn(start, owner, store)
                await self._run_turn(start)
            if not start.keep_alive or (
                self.closed and not self.pending_messages
            ):
                break
            self.stop_requested = False
            if (
                not self.closed
                and self.turns - started_turns >= start.continue_after_turns
            ):
                await self._continue(start, store)
            await workflow.wait_condition(
                lambda: bool(self.pending_messages) or self.closed
            )
        self.closed = True
        await workflow.wait_condition(workflow.all_handlers_finished)
        if store is not None:
            await store._mutate(
                start.system_id, start.thread_id, "release_owner", {}
            )
        self.phase = "stopped" if self.stop_requested else "completed"
        return {
            "answer": self.answer,
            "undelivered": [*self.undelivered, *self.pending_messages],
            "terminal_outcome": self.terminal_outcome,
            "turns": self.turns,
            "formatted_answer": self.formatted_answer,
            "post_error": self.post_error,
            "exit_code": 1 if self.post_error or self.terminal_outcome else 0,
        }

    async def _continue(self, start, store):
        self.phase = "checkpointing_delivery"
        await workflow.wait_condition(workflow.all_handlers_finished)
        if store is not None:
            await store._mutate(
                start.system_id, start.thread_id, "release_owner", {}
            )
        while True:
            await workflow.wait_condition(workflow.all_handlers_finished)
            epoch = self.delivery_epoch
            resume = NativeResume(
                reconciliation_receipts=self.reconciliation_receipts,
                human_replies=self.human_replies,
                reply_receipts=self.reply_receipts,
                formatted_answer=self.formatted_answer,
                post_error=self.post_error,
                closed=self.closed,
                undelivered=self.undelivered,
                accepted=self.accepted,
                pending=self.pending_messages,
                turns=self.turns,
                answer=self.answer,
                terminal_outcome=self.terminal_outcome,
                stop_requested=self.stop_requested,
            )
            raw = encode_resume(resume)
            ref = (
                await save_checkpoint(start, raw)
                if len(raw) > INLINE_BYTES
                else None
            )
            if ref is not None:
                await workflow.wait_condition(workflow.all_handlers_finished)
                if epoch != self.delivery_epoch:
                    continue
            # No await between the final epoch check and CAN: all accepted
            # deliveries are represented, including stop/close during upload.
            workflow.continue_as_new(
                replace(
                    start,
                    messages=[],
                    message_ids=None,
                    resume=None if ref is not None else resume,
                    resume_ref=ref,
                )
            )
