"""MCP calls are supervised by the resource host, not by activity lifetime."""

import asyncio

from gc_core.execution.contracts import ExecutionFailure, ToolResult
from temporalio import workflow

from gc_ua_temporalio.contracts import ToolActivityInput
from gc_ua_temporalio.control_contracts import EffectResult
from gc_ua_temporalio.mcp_contracts import McpCall, McpSessionRef
from gc_ua_temporalio.operations import execute


async def invoke_mcp(operations, invocation, metadata):
    control, start = operations.control, operations.start
    data = McpCall(
        ToolActivityInput(
            start.profile, start.profile_revision, operations.owner, invocation
        ),
        McpSessionRef(**metadata["mcp"]),
        metadata["mcp_tool_name"],
        metadata["mcp_schema"],
    )
    try:
        outcome = await control.run(
            execute(
                "ua.mcp_call",
                data,
                EffectResult,
                queue=start.resource_queue,
                operation_id=invocation.operation_id,
                effect=True,
            )
        )
    except (ExecutionFailure, asyncio.CancelledError):
        outcome = EffectResult(
            "unknown", detail="MCP activity lost its response"
        )
    while outcome.status == "unknown":
        try:
            outcome = await execute(
                "ua.mcp_reconcile",
                data,
                EffectResult,
                queue=start.resource_queue,
            )
        except ExecutionFailure:
            outcome = EffectResult(
                "unknown", detail="MCP reconciliation unavailable"
            )
        if outcome.status == "unknown":
            data.resolution = await control.wait_reconciliation(
                invocation.operation_id, outcome.detail
            )
    if outcome.status == "session_lost":
        control.session.phase = "session_lost"
        control.reconciliation = {
            "operation_id": invocation.operation_id,
            "detail": "SESSION_LOST: start a new session explicitly after checking the old MCP resource",
        }
        await workflow.wait_condition(
            lambda: control.stopped or control.session.closed
        )
        control.stopped = True
        control.session.state.stop_requested = True
        control.reconciliation = None
        return ToolResult(
            '{"ok":false,"error":{"type":"SESSION_LOST"}}',
            outcome="tool_error",
        )
    return outcome.result
