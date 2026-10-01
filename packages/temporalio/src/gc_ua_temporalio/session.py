"""Durable session lifecycle around the unchanged core agent loop."""

import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace

from gc_core.config_modules.intent_scope import IntentScopeConfig
from gc_core.execution.contracts import ExecutionFailure, ModelProviderError
from gc_core.execution.environment import execution_environment
from gc_core.execution.loop import AgentLoop
from gc_core.execution.projection import ThreadProjection
from gc_core.execution.state import ExecutionState
from gc_core.intent_log.user_turn import append_user_ask_deduped
from gc_core.loop_detector.detector import clear as clear_loop_detector
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from gc_ua_temporalio.bindings import build_bindings
from gc_ua_temporalio.contracts import BootstrapRequest, BootstrapSnapshot
from gc_ua_temporalio.control import SessionControl
from gc_ua_temporalio.controlled_operations import ControlledOperations
from gc_ua_temporalio.mcp_contracts import McpSessionRef, ReleaseMcp
from gc_ua_temporalio.operations import execute
from gc_ua_temporalio.session_contracts import (
    Checkpoint,
    InboxRequest,
    MessageDisposition,
    UserMessage,
)
from gc_ua_temporalio.summary import SessionSummary


class SessionEngine:
    def __init__(self, session, start):
        self.session = session
        self.start = start
        self.ready = False
        self.rolling = False
        self.mailbox = []
        self.received = set()
        self.consumed = []
        self.turn_sequence = 0
        self.turns_in_run = 0
        self.cumulative_tool_calls = 0
        self.results = []
        self.accept_lock = asyncio.Lock()
        self.summary = None

    async def submit(self, message):
        if self.rolling:
            raise ApplicationError(
                "Session is continuing; retry the same message",
                type="SessionContinuing",
                non_retryable=True,
            )
        if self.session.closed:
            raise ApplicationError(
                "Session is closing", type="SessionClosing", non_retryable=True
            )
        await workflow.wait_condition(
            lambda: self.ready or self.session.closed
        )
        async with self.accept_lock:
            if self.rolling:
                raise ApplicationError(
                    "Session is continuing; retry the same message",
                    type="SessionContinuing",
                    non_retryable=True,
                )
            if self.session.closed:
                raise ApplicationError(
                    "Session is closing",
                    type="SessionClosing",
                    non_retryable=True,
                )
            return await self._accept(message)

    async def _accept(self, message):
        try:
            receipt = await execute(
                "ua.accept_message",
                InboxRequest(self.session.owner, message),
                dict,
                queue=self.start.resource_queue,
                timeout=30,
            )
        except ExecutionFailure as exc:
            raise ApplicationError(
                "Message was not accepted; verify ID/content and retry with the same ID",
                non_retryable=True,
            ) from exc
        if (
            receipt["disposition"] == "ACCEPTED"
            and message.message_id not in self.received
        ):
            self.received.add(message.message_id)
            self.mailbox.append(message)
        return receipt

    def drain(self):
        if self.session.control.stopped:
            return []
        pending, self.mailbox = self.mailbox, []
        self.consumed.extend(m.message_id for m in pending)
        state = self.session.state
        for message in pending:
            state.current_user_intent = append_user_ask_deduped(
                state.current_user_intent,
                message.text,
                user_turn_seq=state.user_turn_seq,
            )
        return [
            {"id": f"user:{m.message_id}", "role": "user", "content": m.text}
            for m in pending
        ]

    async def run(self):
        await self._initialize()
        with execution_environment(
            new_id=self.session._new_id, now=workflow.now
        ):
            while True:
                await workflow.wait_condition(
                    lambda: self.mailbox or self.session.closed
                )
                if not self.mailbox:
                    break
                await self._turn()
                await workflow.wait_condition(workflow.all_handlers_finished)
                if self.start.auto_close and not self.mailbox:
                    self.session.closed = True
                if not self.session.closed and self._should_continue():
                    await self._continue()
        await self._release_resources()
        self.session.phase = "closed"
        return {
            "answer": self.session.answer,
            "steps": self.session.state.steps,
            "tool_calls": self.cumulative_tool_calls
            if self.start.child_seed
            else self.session.state.tool_calls,
            "thread_id": self.start.thread_id,
            "memory": self.session.state.shared.working_memory,
            "todo": self.session.state.session_todo,
            "usage": self.session.state.usage.to_meta(),
            "disposition": self.results[-1]["disposition"]
            if self.results
            else "PROCESSED",
        }

    async def _release_resources(self):
        references = {
            value["mcp"]["session_id"]: value["mcp"]
            for value in self.snapshot.tool_metadata.values()
            if "mcp" in value
        }
        if references:
            await execute(
                "ua.release_mcp",
                ReleaseMcp(
                    self.session.owner,
                    [McpSessionRef(**r) for r in references.values()],
                ),
                dict,
                queue=self.start.resource_queue,
                timeout=240,
            )

    async def _initialize(self):
        session = self.session
        session.owner = (
            self.start.session_owner
            or f"{workflow.info().workflow_id}/{workflow.info().run_id}"
        )
        restored = None
        if self.start.checkpoint:
            checkpoint = await execute(
                "ua.checkpoint",
                Checkpoint(
                    session.owner, "restore", ref=self.start.checkpoint
                ),
                Checkpoint,
                queue=self.start.resource_queue,
                timeout=30,
            )
            restored = checkpoint.data
            snapshot = BootstrapSnapshot(**restored["snapshot"])
        else:
            snapshot = await execute(
                "ua.bootstrap",
                BootstrapRequest(self.start, session.owner),
                BootstrapSnapshot,
                queue=self.start.resource_queue,
                timeout=30,
                operation_id="bootstrap",
            )
        session.state = ExecutionState.from_execution_snapshot(snapshot.state)
        session.epoch = snapshot.epoch
        session.control = SessionControl(
            session, f"{self.start.thread_id}:turn-1"
        )
        session.operations = ControlledOperations(
            self.start, session.owner, session.control
        )
        session.projection = ThreadProjection(
            system_id=self.start.system_id,
            thread_id=self.start.thread_id,
            meta=snapshot.meta,
            messages=snapshot.history,
            max_seq=snapshot.max_seq,
        )
        self.snapshot = snapshot
        if workflow.patched("ua-compaction-v1"):
            self.summary = SessionSummary(self)
        self.ctx = SimpleNamespace(
            config_dict=snapshot.config,
            tools_data=snapshot.tool_metadata,
            operations=session.operations,
            logger=workflow.logger,
            intent_scope=IntentScopeConfig(),
            model_router=None,
            is_tui=False,
            is_cli_mode=False,
        )
        if restored:
            self._restore(restored)
        else:
            await self._accept(
                UserMessage(
                    self.start.initial_message_id
                    or f"{self.start.thread_id}:initial",
                    self.start.prompt,
                )
            )
        self.ready = True
        session.phase = "idle"

    def _restore(self, restored):
        session = self.session
        session._id_sequence = restored["id_sequence"]
        session._projection_sequence = restored["projection_sequence"]
        session.operations.sequence = restored["operation_sequence"]
        session.answer = restored["answer"]
        self.turn_sequence = restored["turn_sequence"]
        session.control.turn_id = (
            f"{self.start.thread_id}:turn-{self.turn_sequence}"
        )
        self.mailbox = [UserMessage(**m) for m in restored["mailbox"]]
        self.received = {m.message_id for m in self.mailbox}
        self.results = restored["results"]
        self.cumulative_tool_calls = restored.get("cumulative_tool_calls", 0)

    def _loop(self):
        session = self.session
        return AgentLoop(
            session.state,
            self.ctx,
            bindings=build_bindings(workflow.logger),
            get_full_history_messages=None,
            persist_messages_callback=session._persist,
            pre_backend_compaction_hook=self.summary.pre_backend
            if self.summary
            else None,
            turn_end_compaction_hook=self.summary.turn_end
            if self.summary
            else None,
            force_compact_hook=self.summary.force if self.summary else None,
            get_pending_messages=self.drain,
            check_stop=lambda: session.control.stopped,
            session_meta=None,
            mark_finalizing=None,
            mark_finish_gate_checking=None,
        )

    async def _turn(self):
        session = self.session
        self.turn_sequence += 1
        self.turns_in_run += 1
        self.consumed = []
        session.control = SessionControl(
            session, f"{self.start.thread_id}:turn-{self.turn_sequence}"
        )
        session.operations.control = session.control
        state = session.state
        state.steps = state.tool_calls = 0
        state.stop_requested = False
        state.terminal_outcome = None
        state.run_start_usage_meta = state.usage.to_meta()
        if self.start.child_seed:
            self.ctx.config_dict["max_tool_calls"] = max(
                0,
                self.start.child_seed["max_tool_calls"]
                - self.cumulative_tool_calls,
            )
            self.ctx.config_dict["session_token_limit"] = max(
                0,
                self.start.child_seed["session_token_limit"]
                - state.usage.totals()["total"],
            )
        clear_loop_detector(state)
        session.phase = "running"
        if session.parent_stop_requested:
            session.control.stop(session.control.turn_id)
        disposition = "PROCESSED"
        try:
            session.answer = await self._loop().arun(self.drain())
        except (ExecutionFailure, ModelProviderError) as exc:
            session.answer = str(exc)
            disposition = "FAILED"
        if session.control.stopped:
            disposition = "STOPPED"
        if disposition != "PROCESSED":
            state.flush_current_user_intent()
        await session._persist([])
        await self._finish(self.consumed, disposition)
        if disposition in ("STOPPED", "FAILED"):
            await workflow.wait_condition(workflow.all_handlers_finished)
            await self._finish(
                [m.message_id for m in self.mailbox],
                "DROPPED_BY_STOP" if disposition == "STOPPED" else "FAILED",
            )
            self.mailbox.clear()
        self.results = [
            *self.results[-19:],
            {
                "turn_id": session.control.turn_id,
                "disposition": disposition,
                "answer": session.answer,
            },
        ]
        self.cumulative_tool_calls += state.tool_calls
        session.phase = {
            "PROCESSED": "idle",
            "STOPPED": "stopped",
            "FAILED": "failed",
        }[disposition]

    async def _finish(self, ids, disposition):
        if ids:
            await execute(
                "ua.finish_messages",
                MessageDisposition(
                    self.session.owner,
                    ids,
                    disposition,
                    self.session.control.turn_id,
                    self.session.answer,
                ),
                dict,
                queue=self.start.resource_queue,
                timeout=30,
            )

    def _should_continue(self):
        info = workflow.info()
        return (
            self.turns_in_run >= max(1, self.start.continue_after_turns)
            or info.is_continue_as_new_suggested()
            or info.get_current_history_length() > 6000
        )

    async def _continue(self):
        self.rolling = True
        await workflow.wait_condition(workflow.all_handlers_finished)
        session = self.session
        snapshot = replace(
            self.snapshot,
            state=session.state.to_execution_snapshot(),
            meta=session.projection.meta,
            history=session.state.messages,
            max_seq=session.projection.persisted_messages_count,
        )
        data = {
            "snapshot": asdict(snapshot),
            "id_sequence": session._id_sequence,
            "projection_sequence": session._projection_sequence,
            "operation_sequence": session.operations.sequence,
            "turn_sequence": self.turn_sequence,
            "mailbox": [asdict(m) for m in self.mailbox],
            "answer": session.answer,
            "results": self.results,
            "cumulative_tool_calls": self.cumulative_tool_calls,
        }
        saved = await execute(
            "ua.checkpoint",
            Checkpoint(
                session.owner, f"checkpoint-{self.turn_sequence}", data=data
            ),
            Checkpoint,
            queue=self.start.resource_queue,
            timeout=30,
        )
        if not session.closed:
            workflow.continue_as_new(
                replace(
                    self.start,
                    checkpoint=saved.ref,
                    session_owner=session.owner,
                )
            )
