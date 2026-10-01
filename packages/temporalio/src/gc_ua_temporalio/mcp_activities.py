"""Activities wait for host receipts; their death does not destroy MCP state."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from gc_core.execution.contracts import ArtifactRef, ToolResult
from gc_core.tools.connect_mcp.validation import _validate_tool_arguments
from temporalio import activity
from temporalio.converter import value_to_type

from gc_ua_temporalio.control_contracts import EffectResult
from gc_ua_temporalio.guard_requests import digest
from gc_ua_temporalio.guarded_tools import feedback
from gc_ua_temporalio.mcp_contracts import McpCall, ReleaseMcp
from gc_ua_temporalio.mcp_rpc import McpHostClient, McpHostError
from gc_ua_temporalio.preview import commit_preview
from gc_ua_temporalio.storage.guarded_file import workspace_lock
from gc_ua_temporalio.storage.sqlite import encode


class McpActivities:
    def __init__(self, activities):
        self.activities = activities
        self.runtime = activities.runtime
        self.guarded = activities.guarded

    @activity.defn(name="ua.mcp_call")
    async def invoke(self, data: McpCall) -> EffectResult:
        return await self.activities._guarded_call(self._invoke, data)

    @activity.defn(name="ua.mcp_reconcile")
    async def reconcile(self, data: McpCall) -> EffectResult:
        return await self.activities._guarded_call(self._reconcile, data)

    def _input(self, data):
        profile, _ = self.runtime.profile(
            data.tool.profile, data.tool.revision
        )
        if data.session.server not in profile.mcp_servers:
            raise ValueError("MCP server is not enabled")
        invocation = data.tool.invocation
        args = (
            json.loads(invocation.arguments)
            if isinstance(invocation.arguments, str)
            else invocation.arguments
        )
        _validate_tool_arguments(data.tool_name, data.schema, args)
        identity = {
            "owner": data.tool.owner,
            "session_id": data.session.session_id,
            "generation": data.session.generation,
            "operation": invocation.operation_id,
        }
        return identity, {
            **identity,
            "tool": data.tool_name,
            "arguments": args,
        }

    def _cached(self, data):
        row = self.guarded.ledger.get(
            data.tool.owner, data.tool.invocation.operation_id
        )
        if row is not None:
            _, value = self._input(data)
            if row["digest"] != digest(value):
                raise ValueError("MCP operation digest changed")
        if row is None or row["result"] is None:
            return None
        result = self.guarded.ledger.result(row)
        if "mcp_result" in result:
            ref = value_to_type(ArtifactRef, result["mcp_result"])
            payload = json.loads(self.runtime.artifacts.read(ref))
            if payload["owner"] != data.tool.owner:
                raise ValueError("MCP artifact owner mismatch")
            result = ToolResult(encode(payload["result"]), artifacts=[ref])
        else:
            result = value_to_type(ToolResult, result)
        profile, _ = self.runtime.profile(
            data.tool.profile, data.tool.revision
        )
        result = self.runtime._store_large_output(
            result, data.tool, profile.inline_output_chars
        )
        commit_preview(
            self.runtime, data.tool.owner, data.tool.invocation.operation_id
        )
        return EffectResult("complete", result)

    async def _invoke(self, data):
        try:
            identity, value = self._input(data)
        except ValueError as exc:
            return EffectResult(
                "complete", feedback("invalid_arguments", str(exc))
            )
        cached = self._cached(data)
        if cached:
            return cached
        host = McpHostClient(self.runtime.config)
        try:
            await host.request("invoke", **value)
            return await self._poll(host, identity, data)
        except asyncio.CancelledError:
            try:
                await host.request("cancel_operation", **identity)
            except (OSError, McpHostError):
                pass  # Reconciliation must decide the outcome after cancellation.
            raise
        except OSError:
            return EffectResult("unknown", detail="MCP host unavailable")
        except McpHostError as exc:
            return EffectResult(
                "session_lost" if exc.kind == "SESSION_LOST" else "unknown",
                detail=exc.kind,
            )

    async def _poll(self, host, identity, data):
        while True:
            cached = self._cached(data)
            if cached:
                return cached
            status = await host.request("get_operation", **identity)
            if status["status"] != "STARTED":
                return EffectResult("unknown", detail=status["status"])
            await asyncio.sleep(0.5)

    async def _reconcile(self, data):
        cached = self._cached(data)
        if cached:
            return cached
        identity, value = self._input(data)
        profile, _ = self.runtime.profile(
            data.tool.profile, data.tool.revision
        )
        if data.resolution and data.resolution.resolution != "inspect":
            with workspace_lock(self.guarded.locks, profile.workspace):
                # Fence a delayed activity even if the host never saw this call.
                self.guarded.ledger.begin(
                    data.tool.owner,
                    data.tool.invocation.operation_id,
                    str(Path(profile.workspace).resolve()),
                    digest(value),
                    value,
                )
                return self.guarded._resolve_operator(
                    data.tool, data.resolution
                )
        host = McpHostClient(self.runtime.config)
        try:
            status = await host.request("get_operation", **identity)
            if status["status"] == "STARTED":
                return await self._poll(host, identity, data)
            if status["status"] == "NOT_STARTED":
                return self._tombstone(data, value, profile.workspace)
            return EffectResult(
                "unknown", detail="MCP effect has no committed receipt"
            )
        except (OSError, McpHostError):
            return EffectResult(
                "unknown",
                detail="MCP host unavailable; verify effect and session state",
            )

    def _tombstone(self, data, value, workspace):
        with workspace_lock(self.guarded.locks, workspace):
            row = self.guarded.ledger.begin(
                data.tool.owner,
                data.tool.invocation.operation_id,
                workspace,
                digest(value),
                value,
            )
            if row is not None:
                return self._cached(data) or EffectResult(
                    "unknown", detail="A late MCP attempt acquired the fence"
                )
            result = feedback("not_applied", "MCP operation did not start")
            self.guarded.ledger.finish(
                data.tool.owner,
                data.tool.invocation.operation_id,
                "NOT_APPLIED",
                asdict(result),
            )
            return EffectResult("complete", result)

    @activity.defn(name="ua.release_mcp")
    async def release(self, data: ReleaseMcp) -> dict:
        return await self.activities._guarded_call(self._release, data)

    async def _release(self, data):
        host = McpHostClient(self.runtime.config)
        for reference in data.sessions:
            try:
                await host.request(
                    "release",
                    owner=data.owner,
                    session_id=reference.session_id,
                    generation=reference.generation,
                )
            except (OSError, McpHostError):
                # A disconnected host's lease supervisor will clean up after it
                # observes the closed workflow. Never create a replacement here.
                from gc_ua_temporalio.storage.mcp_sessions import McpSessions

                McpSessions(self.runtime.store).status(
                    reference.session_id, "CLOSING"
                )
        return {"released": len(data.sessions)}
