"""Durable human waits, idempotent replies and cooperative turn stop."""

import asyncio
from dataclasses import asdict

from temporalio import workflow
from temporalio.exceptions import ApplicationError


class SessionControl:
    def __init__(self, session, turn_id):
        self.session = session
        self.turn_id = turn_id
        self.stopped = False
        self.pending = None
        self.replies = {}
        self.active_task = None
        self.reconciliation = None
        self.reconcile_requests = {}
        self.reconcile_revision = 0
        self.last_resolution = None

    def validate_reply(self, reply):
        if (
            not reply.reply_id
            or len(reply.reply_id) > 128
            or len(reply.answer) > 4096
        ):
            raise ApplicationError("Invalid human reply", non_retryable=True)
        old = self.replies.get(reply.reply_id)
        if old:
            if old != asdict(reply):
                raise ApplicationError(
                    "Reply ID reused with different content",
                    non_retryable=True,
                )
            return
        if (
            self.stopped
            or not self.pending
            or self.pending["request_id"] != reply.request_id
        ):
            raise ApplicationError(
                "Human request is stale",
                type="StaleHumanReply",
                non_retryable=True,
            )
        if self.pending["kind"] == "confirm" and reply.answer not in (
            "allow_once",
            "deny_once",
        ):
            raise ApplicationError(
                "Use allow_once or deny_once", non_retryable=True
            )
        if any(
            value["request_id"] == reply.request_id
            for value in self.replies.values()
        ):
            raise ApplicationError(
                "Human request already answered", non_retryable=True
            )

    def answer(self, reply):
        self.validate_reply(reply)
        self.replies[reply.reply_id] = asdict(reply)
        return {"accepted": True, "request_id": reply.request_id}

    def stop(self, turn_id):
        if turn_id != self.turn_id:
            raise ApplicationError("Turn ID is stale", non_retryable=True)
        if self.session.phase in ("idle", "stopped", "closed"):
            return {"stopped": self.stopped}
        self.stopped = True
        if self.session.state is not None:
            self.session.state.stop_requested = True
        if self.active_task is not None:
            self.active_task.cancel()
        return {"stopped": True}

    async def run(self, coroutine):
        self.active_task = asyncio.create_task(coroutine)
        try:
            return await self.active_task
        finally:
            self.active_task = None

    async def human(self, operation_id, plan, arguments):
        request_id = f"{operation_id}:{plan.request_digest}"
        self.pending = {
            **asdict(plan.request),
            "request_id": request_id,
            "operation_id": operation_id,
            "digest": plan.request_digest,
            "arguments": arguments,
            "workspace": (plan.before.get("root") or [None])[0],
        }
        self.session.phase = "waiting_human"
        try:
            await workflow.wait_condition(
                lambda: self.stopped or self._reply_for(request_id) is not None
            )
            return None if self.stopped else self._reply_for(request_id)
        finally:
            self.pending = None
            self.session.phase = "running"

    def _reply_for(self, request_id):
        for reply in self.replies.values():
            if reply["request_id"] == request_id:
                return reply["answer"]
        return None

    def request_reconciliation(self, value):
        if value.request_id in self.reconcile_requests:
            if self.reconcile_requests[value.request_id] != asdict(value):
                raise ApplicationError(
                    "Request ID reused for another operation",
                    non_retryable=True,
                )
            return {"accepted": True}
        if (
            not value.request_id
            or len(value.request_id) > 128
            or not self.reconciliation
            or self.reconciliation["operation_id"] != value.operation_id
            or self.session.phase == "session_lost"
        ):
            raise ApplicationError(
                "No matching unresolved operation", non_retryable=True
            )
        if (
            value.resolution not in ("inspect", "applied", "not_applied")
            or len(value.output) > 16000
        ):
            raise ApplicationError("Invalid resolution", non_retryable=True)
        if value.resolution != "inspect" and (
            not value.processes_stopped or not value.output.strip()
        ):
            raise ApplicationError(
                "Resolution requires process termination confirmation and observed outcome",
                non_retryable=True,
            )
        self.reconcile_requests[value.request_id] = asdict(value)
        self.last_resolution = value
        self.reconcile_revision += 1
        return {"accepted": True}

    async def wait_reconciliation(self, operation_id, detail):
        revision = self.reconcile_revision
        self.reconciliation = {"operation_id": operation_id, "detail": detail}
        self.session.phase = "needs_reconciliation"
        try:
            await workflow.wait_condition(
                lambda: self.reconcile_revision != revision
            )
            return self.last_resolution
        finally:
            self.reconciliation = None
            self.session.phase = "running"
