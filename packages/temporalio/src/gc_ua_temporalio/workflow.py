"""Durable multi-turn sessions, with replay branches for earlier histories."""

from __future__ import annotations

from types import SimpleNamespace

from temporalio import workflow
from temporalio.exceptions import ApplicationError

# Pydantic defines config values only; no resource module is passed through.
with workflow.unsafe.imports_passed_through():
    import pydantic  # noqa: F401  # deterministic config types pass through

from gc_core.config_modules.intent_scope import IntentScopeConfig
from gc_core.execution.contracts import ExecutionFailure, ModelProviderError
from gc_core.execution.environment import execution_environment
from gc_core.execution.loop import AgentLoop
from gc_core.execution.projection import ThreadProjection
from gc_core.execution.state import ExecutionState
from gc_core.intent_log.user_turn import append_user_ask_deduped
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper

from gc_ua_temporalio.bindings import build_bindings
from gc_ua_temporalio.contracts import (
    BootstrapRequest,
    BootstrapSnapshot,
    SessionStart,
)
from gc_ua_temporalio.control import SessionControl
from gc_ua_temporalio.control_contracts import HumanReply, ReconcileRequest
from gc_ua_temporalio.controlled_operations import ControlledOperations
from gc_ua_temporalio.operations import TemporalOperations, execute
from gc_ua_temporalio.session import SessionEngine
from gc_ua_temporalio.session_contracts import UserMessage


@workflow.defn(
    failure_exception_types=[ExecutionFailure, ModelProviderError, ValueError]
)
class AgentSessionWorkflow:
    def __init__(self):
        self.phase = "starting"
        self.closed = False
        self.answer = None
        self.state = None
        self._id_sequence = 0
        self._projection_sequence = 0
        self.control = None
        self.engine = None
        self._run_started = False
        self.active_child = None
        self.parent_stop_requested = False

    def _new_id(self, prefix):
        self._id_sequence += 1
        return f"{prefix}-{self._id_sequence}"

    @workflow.query
    def status(self) -> dict:
        result = {
            "phase": self.phase,
            "answer": self.answer,
            "steps": self.state.steps if self.state else 0,
            "tool_calls": self.state.tool_calls if self.state else 0,
            "active_child": self.active_child,
            "projection_sequence": self._projection_sequence,
        }
        if self.control is not None:
            result.update(
                turn_id=self.control.turn_id,
                pending_human=self.control.pending,
                reconciliation=self.control.reconciliation,
                stop_requested=self.control.stopped,
            )
        if self.engine is not None:
            result.update(
                mailbox_size=len(self.engine.mailbox),
                turns=self.engine.results,
                run_id=workflow.info().run_id,
                owner=getattr(self, "owner", None),
                system_id=self.engine.start.system_id,
                thread_id=self.engine.start.thread_id,
            )
        return result

    @workflow.update
    async def submit_message(self, message: UserMessage) -> dict:
        # An Update may be dispatched before run(), including after Continue-As-New.
        await workflow.wait_condition(lambda: self._run_started)
        if self.engine is None:
            raise ApplicationError(
                "This execution does not support multiple turns",
                non_retryable=True,
            )
        return await self.engine.submit(message)

    @workflow.update
    def answer_human(self, reply: HumanReply) -> dict:
        return self.control.answer(reply)

    @answer_human.validator
    def validate_human_reply(self, reply: HumanReply):
        if self.control is None:
            raise ApplicationError(
                "This execution does not support human input",
                non_retryable=True,
            )
        self.control.validate_reply(reply)

    @workflow.update
    async def stop_turn(self, turn_id: str) -> dict:
        if self.control is None:
            raise ApplicationError(
                "This execution does not support stop", non_retryable=True
            )
        result = self.control.stop(turn_id)
        if self.active_child:
            await workflow.get_external_workflow_handle(
                self.active_child
            ).signal("parent_stop")
        return result

    @workflow.signal
    def parent_stop(self):
        self.parent_stop_requested = True
        if self.control:
            self.control.stop(self.control.turn_id)

    @workflow.update
    def reconcile_operation(self, value: ReconcileRequest) -> dict:
        if self.control is None:
            raise ApplicationError(
                "This execution does not support reconciliation",
                non_retryable=True,
            )
        return self.control.request_reconciliation(value)

    @workflow.signal
    def close_session(self):
        self.closed = True

    async def _persist(self, messages):
        self._projection_sequence += 1
        meta = ThreadMetaMapper.merge_state_into_meta(
            self.projection.meta, self.state
        )
        meta.update(
            previous_response_id=self.state.previous_response_id,
            last_backend_response_id=self.state.last_backend_response_id,
            steps=self.state.steps,
            tool_calls=self.state.tool_calls,
        )
        batch = self.projection.prepare_projection(
            owner=self.owner,
            epoch=self.epoch,
            event_id=f"persist-{self._projection_sequence}",
            sequence=self._projection_sequence,
            messages=messages,
            meta=meta,
        )
        ack = await self.operations.persist(batch)
        self.projection.apply_projection_ack(batch, ack)

    @workflow.run
    async def run(self, start: SessionStart) -> dict:
        self._run_started = True
        if workflow.patched("ua-session-v2"):
            self.engine = SessionEngine(self, start)
            return await self.engine.run()
        self.owner = f"{workflow.info().workflow_id}/{workflow.info().run_id}"
        if workflow.patched("ua-controls-v1"):
            self.control = SessionControl(self, f"{start.thread_id}:turn-1")
        snapshot = await execute(
            "ua.bootstrap",
            BootstrapRequest(start, self.owner),
            BootstrapSnapshot,
            queue=start.resource_queue,
            timeout=30,
            operation_id="bootstrap",
        )
        self.state = ExecutionState.from_execution_snapshot(snapshot.state)
        if self.control and self.control.stopped:
            self.state.stop_requested = True
        self.epoch = snapshot.epoch
        self.operations = (
            ControlledOperations(start, self.owner, self.control)
            if self.control
            else TemporalOperations(start, self.owner)
        )
        self.projection = ThreadProjection(
            system_id=start.system_id,
            thread_id=start.thread_id,
            meta=snapshot.meta,
            messages=snapshot.history,
            max_seq=snapshot.max_seq,
        )
        ctx = SimpleNamespace(
            config_dict=snapshot.config,
            tools_data=snapshot.tool_metadata,
            operations=self.operations,
            logger=workflow.logger,
            intent_scope=IntentScopeConfig(),
            model_router=None,
            is_tui=False,
            is_cli_mode=False,
        )
        loop = AgentLoop(
            self.state,
            ctx,
            bindings=build_bindings(workflow.logger),
            get_full_history_messages=None,
            persist_messages_callback=self._persist,
            pre_backend_compaction_hook=None,
            turn_end_compaction_hook=None,
            force_compact_hook=None,
            get_pending_messages=None,
            check_stop=(lambda: self.control.stopped)
            if self.control
            else None,
            session_meta=None,
            mark_finalizing=None,
            mark_finish_gate_checking=None,
        )
        self.phase = "running"
        with execution_environment(new_id=self._new_id, now=workflow.now):
            user = {
                "id": self._new_id("user"),
                "role": "user",
                "content": start.prompt,
            }
            self.state.current_user_intent = append_user_ask_deduped(
                self.state.current_user_intent,
                start.prompt,
                user_turn_seq=self.state.user_turn_seq,
            )
            self.answer = await loop.arun([user])
            await self._persist([])
        self.phase = (
            "stopped" if self.control and self.control.stopped else "idle"
        )
        if not start.auto_close:
            await workflow.wait_condition(lambda: self.closed)
        self.phase = "closed"
        return {
            "answer": self.answer,
            "steps": self.state.steps,
            "tool_calls": self.state.tool_calls,
            "memory": self.state.shared.working_memory,
            "todo": self.state.session_todo,
            "usage": self.state.usage.to_meta(),
            "thread_id": start.thread_id,
        }
