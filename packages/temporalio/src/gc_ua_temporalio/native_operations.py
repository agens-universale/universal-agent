"""Temporal transport for native core resource ports."""

import asyncio
from dataclasses import asdict
from datetime import timedelta

from gc_core.execution.contracts import (
    ExecutionFailure,
    GuardPreconditionChanged,
    InfrastructureFailure,
    OutcomeUnknown,
    ToolResult,
)
from gc_core.execution.guard_contracts import GuardProbeResult
from gc_core.execution.resource_contracts import ResourceOwnerLost
from gc_core.project_memory.operations import ProjectMemoryReply
from gc_core.run_preparation import PreparedPrompts
from gc_core.state_preparation import PreparedState
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
)

from gc_ua_temporalio.native_contracts import NativeCall, NativeModelReply
from gc_ua_temporalio.native_model_routing import public_router_identity


def activity_summary(name, data):
    """Readable UI labels without prompts, file contents or credentials."""
    if name == "agent.model.request":
        request = data["request"]
        return f"Model: {request['request_label']} ({request['consumer']})"
    if name == "agent.tool.execute":
        return f"Tool: {data['name']}"
    if name == "agent.memory.execute":
        return f"Project memory: {data['action']} / {data['phase']}"
    if name == "agent.guard.probe":
        return f"Guard: {data['kind']}"
    return name.removeprefix("agent.").replace(".", " / ")


class NativeOperations:
    def __init__(self, start, owner_id, new_id):
        self.start, self.owner_id, self.new_id = start, owner_id, new_id
        self.router = None
        self.resource_owner = None
        self.child_context = None
        self._prompts = {}
        self.closed = False
        self.released = False

    def next_operation_id(self, kind):
        return self.new_id(kind)

    def monotonic(self):
        return workflow.time()

    async def call(
        self, name, data, result_type, *, operation_id=None, model=False
    ):
        request = NativeCall(
            self.start.profile,
            self.start.revision,
            self.owner_id,
            operation_id or self.new_id(name),
            data,
            resource_owner=self.resource_owner,
            child_context=self.child_context,
        )
        try:
            return await workflow.execute_activity(
                name,
                request,
                result_type=result_type,
                activity_id=request.operation_id,
                summary=activity_summary(name, data),
                task_queue=self.start.model_queue
                if model
                else self.start.resource_queue,
                start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
        except ActivityError as exc:
            return await self._activity_error(exc, name, request, result_type)

    async def _activity_error(self, exc, name, request, result_type):
        if isinstance(exc.cause, CancelledError):
            raise asyncio.CancelledError(str(exc.cause)) from exc
        errors = {
            cls.__name__: cls
            for cls in (
                ExecutionFailure,
                GuardPreconditionChanged,
                InfrastructureFailure,
                OutcomeUnknown,
                ResourceOwnerLost,
            )
        }
        if (
            isinstance(exc.cause, ApplicationError)
            and exc.cause.type in errors
        ):
            if exc.cause.type == "OutcomeUnknown":
                waiter = getattr(self, "reconciliation_wait", None)
                if waiter is not None:
                    from temporalio.converter import value_to_type

                    return value_to_type(
                        result_type, await waiter(name, request)
                    )
            raise errors[exc.cause.type](exc.cause.message) from exc
        raise InfrastructureFailure(f"Native activity failed: {name}") from exc

    async def model(self, request):
        provider = getattr(self, "router_provider", None)
        router = provider() if provider is not None else self.router
        self.router = router
        reply = await self.call(
            "agent.model.request",
            {
                "request": asdict(request),
                "circuit_breaker": router.breaker.snapshot(),
                "routing": public_router_identity(router),
            },
            NativeModelReply,
            operation_id=request.operation_id,
            model=True,
        )
        router.breaker.replace_from(reply.circuit_breaker)
        return reply.result

    async def tool(self, _invocation):
        raise ExecutionFailure(
            "Native tools must use the shared guard and leaf-effect path"
        )

    async def tool_effect(self, request):
        return await self.call(
            "agent.tool.execute",
            asdict(request),
            ToolResult,
            operation_id=request.operation_id,
        )

    async def guard_probe(self, request):
        return await self.call(
            "agent.guard.probe", asdict(request), GuardProbeResult
        )

    async def state_preparer(self, request):
        return await self.call(
            "agent.state.prepare", asdict(request), PreparedState
        )

    async def prompt_loader(self, request):
        return await self.call(
            "agent.prompts.prepare", asdict(request), PreparedPrompts
        )

    async def prompt_resource(self, request):
        if request not in self._prompts:
            self._prompts[request] = await self.call(
                "agent.prompt.read", asdict(request), str
            )
        return self._prompts[request]

    async def skills_loader(self):
        return await self.call("agent.skills.list", {}, list[dict])

    async def project_memory_io(self, request):
        return await self.call(
            "agent.memory.execute", asdict(request), ProjectMemoryReply
        )

    async def project_memory_hash(self):
        return await self.call("agent.memory.hash", {}, str | None)

    async def summary_trace_sink(self, record):
        await self.call("agent.summary.trace", asdict(record), bool)

    async def mcp_cleanup(self):
        if not self.closed:
            await self.call("agent.context.close", {}, bool)
            self.closed = True

    async def release_context(self):
        if not self.released:
            await self.call("agent.context.release", {}, bool)
            self.released = True
