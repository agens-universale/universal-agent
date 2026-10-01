"""Real MCP transports in a separate host; clients can disconnect independently."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from gc_core.execution.contracts import ArtifactRef
from gc_ua_temporalio.config import AgentProfile, TemporalRuntimeConfig
from gc_ua_temporalio.mcp_config import McpServerProfile
from gc_ua_temporalio.mcp_rpc import McpHostClient, McpHostError
from gc_ua_temporalio.storage.artifacts import ArtifactStore
from gc_ua_temporalio.storage.effects import EffectLedger
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from temporalio.testing import WorkflowEnvironment


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HostStand:
    def __init__(self, tmp_path, address, transport, monkeypatch):
        self.folder = tmp_path
        self.processes = []
        self.logs = []
        self.fixture = Path(__file__).with_name("mcp_fixture_server.py")
        self.server_port = None
        monkeypatch.setenv("GC_UA_MCP_TEST_TOKEN", uuid.uuid4().hex)
        port = free_port()
        if transport in ("stdio", "stream_stdio"):
            server = McpServerProfile(
                transport=transport,
                persistent_session=True,
                command=sys.executable,
                args=[str(self.fixture), "stdio", "0"],
            )
        else:
            self.server_port = port
            mode = (
                "sse"
                if transport in ("sse", "stream_sse")
                else "streamable-http"
            )
            self.spawn([sys.executable, str(self.fixture), mode, str(port)])
            server = McpServerProfile(
                transport=transport,
                persistent_session=True,
                url=f"http://127.0.0.1:{port}",
            )
        self.config = TemporalRuntimeConfig(
            address=address,
            database=str(tmp_path / "db"),
            artifacts=str(tmp_path / "artifacts"),
            mcp_host_port=free_port(),
            mcp_host_token_env="GC_UA_MCP_TEST_TOKEN",
            mcp_servers={"counter": server},
            profiles={
                "demo": AgentProfile(
                    workspace=str(tmp_path),
                    model="fixture",
                    completions_url="http://127.0.0.1:1",
                    mcp_servers=["counter"],
                )
            },
        )
        self.path = tmp_path / "runtime.json"
        self.path.write_text(self.config.model_dump_json())

    def spawn(self, command):
        log = (self.folder / f"process-{len(self.logs)}.log").open("w")
        self.logs.append(log)
        process = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT
        )
        self.processes.append(process)
        return process

    async def start(self):
        if self.server_port:
            async with asyncio.timeout(20):
                while True:
                    try:
                        _, ready = await asyncio.open_connection(
                            "127.0.0.1", self.server_port
                        )
                        ready.close()
                        await ready.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(0.1)
        process = self.spawn(
            [
                sys.executable,
                "-m",
                "gc_ua_temporalio.mcp_host",
                "--config",
                str(self.path),
            ]
        )
        async with asyncio.timeout(30):
            while True:
                if process.poll() is not None:
                    raise AssertionError(
                        "MCP host exited: "
                        + (
                            self.folder / f"process-{len(self.logs) - 1}.log"
                        ).read_text()
                    )
                try:
                    _, writer = await asyncio.open_connection(
                        "127.0.0.1", self.config.mcp_host_port
                    )
                    writer.close()
                    await writer.wait_closed()
                    return process
                except OSError:
                    await asyncio.sleep(0.1)

    def close(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in self.logs:
            log.close()


async def acquire(client, owner):
    result = await client.request(
        "acquire", owner=owner, profile="demo", revision="v1", server="counter"
    )
    return {
        "owner": owner,
        "session_id": result["session_id"],
        "generation": result["generation"],
    }


async def result(client, identity, operation, stand):
    async with asyncio.timeout(25):
        while True:
            value = await client.request(
                "get_operation", **identity, operation=operation
            )
            if value["status"] == "SUCCEEDED":
                ref = ArtifactRef(**value["result"]["mcp_result"])
                raw = json.loads(
                    ArtifactStore(stand.config.artifacts).read(ref)
                )["result"]
                raw = raw.get("result", raw)
                return json.loads(raw["content"][0]["text"])
            assert value["status"] == "STARTED", value
            await asyncio.sleep(0.1)


@pytest.mark.parametrize(
    "transport",
    ["stdio", "stream_stdio", "sse", "stream_sse", "http", "stream_http"],
)
async def test_host_keeps_session_and_deduplicates_across_clients(
    tmp_path, monkeypatch, transport
):
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=shutil.which("temporal")
    ) as env:
        stand = HostStand(
            tmp_path,
            env.client.service_client.config.target_host,
            transport,
            monkeypatch,
        )
        try:
            await stand.start()
            client = McpHostClient(stand.config)
            identity = await acquire(client, "workflow-A/run-A")
            assert await acquire(client, "workflow-A/run-A") == identity
            await client.request(
                "invoke",
                **identity,
                operation="one",
                tool="increment",
                arguments={"delay": 0.5},
            )
            # A different client reconnects while the host is executing the call.
            client = McpHostClient(stand.config)
            await client.request(
                "invoke",
                **identity,
                operation="one",
                tool="increment",
                arguments={"delay": 0.5},
            )
            first = await result(client, identity, "one", stand)
            assert first["count"] == 1
            if transport != "stdio":
                with SQLiteThreadStore(
                    stand.config.database
                ).transaction() as db:
                    events = db.execute(
                        "SELECT kind,payload FROM view_events WHERE owner=? AND operation='one' ORDER BY cursor",
                        (identity["owner"],),
                    ).fetchall()
                assert [e["kind"] for e in events] == [
                    "reset",
                    "preview",
                    "preview",
                    "committed",
                ]
                assert (
                    "increment started"
                    in json.loads(events[1]["payload"])["text"]
                )
                assert (
                    "increment complete"
                    in json.loads(events[2]["payload"])["text"]
                )
            await client.request(
                "invoke",
                **identity,
                operation="two",
                tool="increment",
                arguments={},
            )
            second = await result(client, identity, "two", stand)
            assert (
                second["count"] == 2 and second["session"] == first["session"]
            )
            neighbor = await acquire(client, "workflow-B/run-B")
            await client.request(
                "invoke",
                **neighbor,
                operation="one",
                tool="increment",
                arguments={},
            )
            assert (await result(client, neighbor, "one", stand))["count"] == 1
            await client.request("release", **identity)
            await client.request(
                "invoke",
                **neighbor,
                operation="two",
                tool="increment",
                arguments={},
            )
            assert (await result(client, neighbor, "two", stand))["count"] == 2
            await client.request("release", **neighbor)
        finally:
            stand.close()


async def test_host_loss_is_explicit_and_does_not_reinitialize(
    tmp_path, monkeypatch
):
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=shutil.which("temporal")
    ) as env:
        stand = HostStand(
            tmp_path,
            env.client.service_client.config.target_host,
            "stream_stdio",
            monkeypatch,
        )
        old_pid = None
        try:
            host = await stand.start()
            client = McpHostClient(stand.config)
            identity = await acquire(client, "workflow-A/run-A")
            await client.request(
                "invoke",
                **identity,
                operation="one",
                tool="increment",
                arguments={},
            )
            old_pid = (await result(client, identity, "one", stand))["pid"]
            host.kill()
            host.wait()
            await stand.start()
            with pytest.raises(McpHostError) as error:
                await acquire(client, "workflow-A/run-A")
            assert error.value.kind == "SESSION_LOST"
            store = SQLiteThreadStore(stand.config.database)
            assert (
                EffectLedger(store).get("workflow-A/run-A", "one")["status"]
                == "SUCCEEDED"
            )
        finally:
            stand.close()
            if old_pid:
                try:
                    os.kill(old_pid, 15)
                except ProcessLookupError:
                    pass


async def test_sse_server_disconnect_has_unknown_outcome_and_no_new_session(
    tmp_path, monkeypatch
):
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=shutil.which("temporal")
    ) as env:
        stand = HostStand(
            tmp_path,
            env.client.service_client.config.target_host,
            "stream_sse",
            monkeypatch,
        )
        try:
            await stand.start()
            client = McpHostClient(stand.config)
            identity = await acquire(client, "workflow-A/run-A")
            await client.request(
                "invoke",
                **identity,
                operation="disconnect",
                tool="increment",
                arguments={"delay": 10},
            )
            ledger = EffectLedger(SQLiteThreadStore(stand.config.database))
            async with asyncio.timeout(20):
                while ledger.get(identity["owner"], "disconnect") is None:
                    await asyncio.sleep(0.1)
            stand.processes[0].kill()  # MCP application, not activity worker.
            stand.processes[0].wait()
            async with asyncio.timeout(20):
                while (
                    await client.request(
                        "get_operation", **identity, operation="disconnect"
                    )
                )["status"] == "STARTED":
                    await asyncio.sleep(0.1)
            with pytest.raises(McpHostError) as error:
                await client.request(
                    "invoke",
                    **identity,
                    operation="next",
                    tool="increment",
                    arguments={},
                )
            assert error.value.kind == "SESSION_LOST"
            assert ledger.get(identity["owner"], "next") is None
        finally:
            stand.close()
