"""Temporal delivery and waiting around the ordinary AgentExecutor lifecycle."""

import asyncio
import hashlib
from dataclasses import asdict, replace

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    import pydantic  # noqa: F401

from gc_core.agent_executor import AgentExecutor
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

from gc_ua_temporalio.native_child_transport import install_native_subagents
from gc_ua_temporalio.native_context import assemble_native_context
from gc_ua_temporalio.native_contracts import (
    NativePreparedContext,
    NativeResume,
    NativeRunRequest,
    UserMessage,
)
from gc_ua_temporalio.native_operations import NativeOperations
from gc_ua_temporalio.thread_store_proxy import TemporalThreadStore
from gc_ua_temporalio.thread_store_transport import ThreadStoreTransport

#: Сколько последних ID сообщений и ответов помнить для дедупликации.
DEDUP_WINDOW = 1000


def _message_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _recent(values):
    """Последние ``DEDUP_WINDOW`` записей: состояние доставки не растёт вечно."""
    if isinstance(values, dict):
        return dict(list(values.items())[-DEDUP_WINDOW:])
    return list(values)[-DEDUP_WINDOW:]


@workflow.defn(
    name="NativeAgentWorkflow",
    failure_exception_types=[Exception],
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
        self.turns = 0
        self.answer = None
        self.formatted_answer = None
        self.post_error = None
        self.terminal_outcome = None
        self.resumed_store = None

    def new_id(self, prefix):
        self.sequence += 1
        digest = hashlib.sha256(
            f"{self.run_identity}/{prefix}/{self.sequence}".encode()
        ).hexdigest()
        return f"{prefix}_{digest}"

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

    async def human_request(self, request, *, flush=True):
        # Ожидание может длиться часами: шаги и сам вопрос — в тред до него.
        # Вопрос ребёнка приходит сигналом, параллельно с основным циклом:
        # там тред родителя не трогаем, он запишется в конце его хода.
        if flush and self.executor is not None:
            await self.executor.aflush_thread()
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
        if start.schema_version != 1:
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
        if resume.store_owner_epoch is not None:
            self.resumed_store = (
                resume.store_owner_epoch,
                resume.store_generation or 0,
            )
        self.human_replies = dict(resume.human_replies)
        self.reply_receipts = dict(resume.reply_receipts)
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
            if start.keep_alive:
                # Сессия, открытая Update-With-Start: ввод придёт update'ом.
                return
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
        self.operations.tool_metadata = prepared.tool_metadata
        self.operations.prepared = prepared
        if store is None and self.resumed_store is not None:
            epoch, generation = self.resumed_store
            store = TemporalThreadStore(
                start.store_id,
                ThreadStoreTransport(task_queue=start.resource_queue),
                owner_id=owner,
                owner_epoch=epoch,
                generation=generation,
                capabilities=prepared.store_capabilities,
                threads_dir=prepared.threads_dir,
            )
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
            epoch = snapshot.owner_epoch
            if snapshot.owner_id != owner:
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
                epoch = receipt.owner_epoch
            store.owner_id, store.owner_epoch = owner, epoch
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
            human_request=lambda request: self.human_request(
                request, flush=False
            ),
        )
        self.ctx = ctx
        self.executor = AgentExecutor(ctx)
        self.children.redeliver = self.executor.add_message
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
                # stop/ошибка: отложенные шаги не должны остаться только в
                # истории workflow.
                await self.executor.aflush_thread()
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
        # Владелец треда — сессия целиком: Continue-As-New его не меняет.
        owner = workflow.info().workflow_id
        store = None
        while True:
            ran = False
            if self.pending_messages:
                store = await self._prepare_turn(start, owner, store)
                await self._run_turn(start)
                ran = True
            if not start.keep_alive or (
                self.closed and not self.pending_messages
            ):
                break
            self.stop_requested = False
            if ran and not self.closed and not self.pending_messages:
                # Ход закончен: новый run начинается с почти пустой историей.
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
        self.phase = "continuing"
        await workflow.wait_condition(workflow.all_handlers_finished)
        if self.pending_messages or self.closed:
            # Пока ждали обработчики, пришёл ввод: сначала обслужить его.
            self.phase = "idle"
            return
        resume = NativeResume(
            human_replies=_recent(self.human_replies),
            reply_receipts=_recent(self.reply_receipts),
            formatted_answer=self.formatted_answer,
            post_error=self.post_error,
            closed=self.closed,
            undelivered=_recent(self.undelivered),
            accepted=_recent(self.accepted),
            pending=self.pending_messages,
            turns=self.turns,
            answer=self.answer,
            terminal_outcome=self.terminal_outcome,
            store_owner_epoch=store.owner_epoch if store else None,
            store_generation=store._generation if store else None,
        )
        # Без await между проверкой и CAN: весь принятый ввод уже в resume.
        workflow.continue_as_new(
            replace(start, messages=[], message_ids=None, resume=resume)
        )
