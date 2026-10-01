"""Separate resource host: MCP lifetime is independent of Temporal activities."""

import argparse
import asyncio
import hmac
import json
import os
import time
import uuid
from pathlib import Path

from temporalio.client import Client

from gc_ua_temporalio.config import TemporalRuntimeConfig
from gc_ua_temporalio.guard_requests import digest
from gc_ua_temporalio.mcp_actor import McpActor
from gc_ua_temporalio.mcp_rpc import McpHostError
from gc_ua_temporalio.runtime import WorkerRuntime
from gc_ua_temporalio.storage.effects import EffectLedger
from gc_ua_temporalio.storage.mcp_sessions import McpSessions


class McpSessionHost:
    def __init__(self, runtime):
        self.runtime = runtime
        self.config = runtime.config
        self.sessions = McpSessions(runtime.store)
        self.effects = EffectLedger(runtime.store)
        self.artifacts = runtime.artifacts
        self.locks = Path(self.config.artifacts) / "workspace-locks"
        self.generation = str(uuid.uuid4())
        self.actors = {}
        self.acquire_lock = asyncio.Lock()
        if not self.config.mcp_host_token_env:
            raise ValueError("MCP host requires mcp_host_token_env")
        self.token = os.environ[self.config.mcp_host_token_env]
        if not self.token:
            raise ValueError("MCP host token must be nonempty")

    async def acquire(self, *, owner, profile, revision, server):
        agent, ctx = self.runtime.profile(profile, revision)
        if server not in agent.mcp_servers:
            raise ValueError("MCP server is not enabled in this agent profile")
        settings = self.config.mcp_servers[server]
        workspace = str(Path(agent.workspace).resolve())
        session_id = digest([owner, server, settings.revision, workspace])
        async with self.acquire_lock:
            row = self.sessions.get(session_id)
            if row is None:
                self.sessions.create(
                    session_id,
                    owner,
                    server,
                    settings.revision,
                    workspace,
                    str(uuid.uuid4()),
                    self.generation,
                    self.config.mcp_lease_seconds,
                )
                row = self.sessions.get(session_id)
                actor = McpActor(self, row, settings, ctx.os_backend)
                self.actors[session_id] = actor
                await actor.ready
            return self.describe(
                session_id=session_id,
                generation=row["generation"],
                owner=owner,
            )

    def _row(self, session_id, generation, owner):
        row = self.sessions.get(session_id)
        if not row or row["owner"] != owner or row["generation"] != generation:
            raise ValueError("MCP session identity does not match")
        return row

    def describe(self, *, session_id, generation, owner):
        row = self._row(session_id, generation, owner)
        if row["host"] != self.generation or row["status"] != "READY":
            raise McpHostError(
                "SESSION_LOST",
                "The original MCP session is unavailable; it was not replaced",
            )
        self.sessions.renew(session_id, self.config.mcp_lease_seconds)
        return self.sessions.describe(row)

    def invoke(
        self, *, session_id, generation, owner, operation, tool, arguments
    ):
        self.describe(
            session_id=session_id, generation=generation, owner=owner
        )
        value = {
            "owner": owner,
            "operation": operation,
            "session_id": session_id,
            "generation": generation,
            "tool": tool,
            "arguments": arguments,
        }
        value["digest"] = digest(value)
        row = self.effects.get(owner, operation)
        if row is not None:
            if row["digest"] != value["digest"]:
                raise ValueError(
                    "MCP operation ID reused with different content"
                )
            return self._operation(row)
        actor = self.actors[session_id]
        if (
            operation in actor.pending
            and actor.pending[operation] != value["digest"]
        ):
            raise ValueError("MCP operation ID reused with different content")
        if operation not in actor.pending:
            actor.pending[operation] = value["digest"]
            actor.queue.put_nowait(value)
        return {"status": "STARTED"}

    def get_operation(self, *, session_id, generation, owner, operation):
        self._row(session_id, generation, owner)
        row = self.effects.get(owner, operation)
        if row is not None:
            actor = self.actors.get(session_id)
            if row["status"] == "STARTED" and (
                actor is None or actor.lost or actor.current != operation
            ):
                return {"status": "UNKNOWN"}
            return self._operation(row)
        actor = self.actors.get(session_id)
        if actor and operation in actor.pending:
            return {"status": "UNKNOWN" if actor.lost else "STARTED"}
        return {"status": "NOT_STARTED"}

    @staticmethod
    def _operation(row):
        return {"status": row["status"], "result": EffectLedger.result(row)}

    async def cancel_operation(
        self, *, session_id, generation, owner, operation
    ):
        self._row(session_id, generation, owner)
        actor = self.actors.get(session_id)
        if actor and operation in actor.pending:
            await actor.close(cancel=True)
        return self.get_operation(
            session_id=session_id,
            generation=generation,
            owner=owner,
            operation=operation,
        )

    async def release(self, *, session_id, generation, owner):
        self._row(session_id, generation, owner)
        actor = self.actors.pop(session_id, None)
        if actor:
            await actor.close()
        return {"released": True}

    async def connection(self, reader, writer):
        try:
            async with asyncio.timeout(45):
                data = json.loads(await reader.readline())
                if not hmac.compare_digest(
                    str(data.get("token", "")), self.token
                ):
                    raise ValueError("Unauthorized host request")
                method = data.get("method")
                if method not in (
                    "acquire",
                    "describe",
                    "invoke",
                    "get_operation",
                    "cancel_operation",
                    "release",
                ):
                    raise ValueError("Unknown host method")
                result = getattr(self, method)(**data["params"])
                if asyncio.iscoroutine(result):
                    result = await result
                reply = {"result": result}
        except Exception as exc:
            reply = {
                "error": getattr(exc, "kind", type(exc).__name__),
                "detail": type(exc).__name__,
            }
        try:
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def supervise(self, client):
        while True:
            await asyncio.sleep(30)
            for session_id, actor in list(self.actors.items()):
                row = self.sessions.get(session_id)
                if row["expires"] > time.time():
                    continue
                workflow_id = row["owner"].rsplit("/", 1)[0]
                try:
                    description = await client.get_workflow_handle(
                        workflow_id
                    ).describe()
                except Exception:
                    continue  # A server outage is not permission to drop state.
                if description.status.name == "RUNNING":
                    self.sessions.renew(
                        session_id, self.config.mcp_lease_seconds
                    )
                else:
                    await actor.close(cancel=True)
                    self.actors.pop(session_id, None)


async def serve(config):
    host = McpSessionHost(WorkerRuntime(config))
    client = await Client.connect(config.address, namespace=config.namespace)
    supervisor = asyncio.create_task(host.supervise(client))
    server = await asyncio.start_server(
        host.connection, "127.0.0.1", config.mcp_host_port, limit=1_048_576
    )
    try:
        async with server:
            await server.serve_forever()
    finally:
        supervisor.cancel()
        for actor in host.actors.values():
            await actor.close(cancel=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    asyncio.run(serve(TemporalRuntimeConfig.load(args.config)))


if __name__ == "__main__":
    main()
