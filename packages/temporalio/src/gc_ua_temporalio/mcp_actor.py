"""One owner task owns the MCP context managers throughout a logical session."""

import asyncio
import logging
from dataclasses import asdict

from gc_core.mcp_session_owner import use_owner
from gc_core.tools.connect_mcp.durable_scope import (
    durable_mcp_calls,
    mcp_progress,
)
from gc_core.tools.connect_mcp.execute_on import resolve_mcp_client
from gc_core.tools.connect_mcp.persistent_stdio import PersistentStdioRegistry
from gc_core.tools.connect_mcp.stream_client import _SESSION_MANAGER
from gc_core.tools.connect_mcp.validation import format_mcp_tool_result

from gc_ua_temporalio.preview import PreviewSink, commit_preview
from gc_ua_temporalio.storage.guarded_file import workspace_lock
from gc_ua_temporalio.storage.sqlite import encode


class McpActor:
    def __init__(self, host, row, profile, backend):
        self.host, self.row, self.profile = host, row, profile
        self.client = resolve_mcp_client(
            row["server"],
            profile.client_config(row["workspace"]),
            os_backend=backend,
        )
        self.queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.pending = {}
        self.current = None
        self.lost = False
        self.task = asyncio.create_task(self.run())

    async def run(self):
        with use_owner(self.row["id"]), durable_mcp_calls():
            try:
                tools = await self.client.alist_tools(timeout=30)
                self.host.sessions.status(self.row["id"], "READY", tools)
                self.ready.set_result(tools)
                while not self.lost:
                    value = await self.queue.get()
                    if value is None:
                        break
                    await self._invoke(value)
            except BaseException as exc:
                self.lost = True
                if not self.ready.done():
                    logging.getLogger(__name__).exception(
                        "MCP discovery failed: %s", self.row["server"]
                    )
                    self.ready.set_exception(RuntimeError(type(exc).__name__))
            finally:
                self.host.sessions.status(
                    self.row["id"], "SESSION_LOST" if self.lost else "CLOSED"
                )
                await _SESSION_MANAGER.aclose_all(in_owner_task=True)
                await PersistentStdioRegistry.instance().aclose_all()

    async def _invoke(self, value):
        owner, operation = value["owner"], value["operation"]
        self.current = operation
        try:
            with workspace_lock(self.host.locks, self.row["workspace"]):
                old = self.host.effects.begin(
                    owner,
                    operation,
                    self.row["workspace"],
                    value["digest"],
                    value,
                )
                if old is not None:
                    return
                await self._call(value)
        except BaseException:
            if self.host.effects.get(owner, operation):
                self.host.effects.finish(owner, operation, "UNKNOWN")
            self.lost = True
        finally:
            self.pending.pop(operation, None)
            self.current = None

    async def _call(self, value):
        # No retry/fallback wrapper around the MCP port. The host owns the call
        # after the RPC client or activity worker has disappeared.
        preview = PreviewSink(
            self.host.runtime, value["owner"], value["operation"], attempt=1
        )
        with mcp_progress(preview.mcp_progress):
            async with asyncio.timeout(self.profile.timeout_seconds):
                raw = await self.client.acall_tool(
                    value["tool"],
                    value["arguments"],
                    timeout=self.profile.timeout_seconds,
                )
        raw = format_mcp_tool_result(raw)
        artifact = self.host.artifacts.put(
            encode({"owner": value["owner"], "result": raw}).encode()
        )
        # Ledger stores an immutable reference; binary outputs never enter RPC.
        self.host.effects.finish(
            value["owner"],
            value["operation"],
            "SUCCEEDED",
            {"mcp_result": asdict(artifact)},
        )
        commit_preview(self.host.runtime, value["owner"], value["operation"])

    async def close(self, *, cancel=False):
        if cancel:
            self.task.cancel()
        else:
            self.queue.put_nowait(None)
        try:
            await self.task
        except asyncio.CancelledError:
            if not self.task.done():
                raise
