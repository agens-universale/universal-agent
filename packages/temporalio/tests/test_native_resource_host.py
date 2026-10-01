"""Native resource owner lifecycle using the real persisted effect ledger."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from gc_core.execution.contracts import OutcomeUnknown, ToolEffectRequest
from gc_core.execution.resource_contracts import ResourceOwnerLost
from gc_core.execution.resource_host import WorkerResourceHost
from gc_core.execution.tool_effects import arguments_digest
from gc_core.mcp_session_owner import current_owner
from gc_ua_temporalio.storage.effects import EffectLedger
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore


async def no_close():
    pass


def request(operation="one", arguments=None):
    arguments = arguments or {}
    return ToolEffectRequest(
        operation,
        "test_resource",
        None,
        arguments,
        arguments_digest(arguments),
    )


def context(callback):
    class Tool:
        def __init__(self, **_kwargs):
            pass

        async def acall(self, **arguments):
            return await callback(arguments)

    return SimpleNamespace(
        config_dict={},
        tools_data={
            "test_resource": {
                "tool_fabric": Tool,
                "spec": {
                    "name": "test_resource",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        },
    )


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_repeat_and_close_stays_in_owner_task(
    tmp_path,
):
    ledger = EffectLedger(SQLiteThreadStore(tmp_path / "db"))
    host = WorkerResourceHost("worker", ledger=ledger)
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    async def call(_arguments):
        events.append(("call", asyncio.current_task(), current_owner()))
        entered.set()
        await release.wait()
        return "done"

    async def bootstrap():
        events.append(("open", asyncio.current_task(), current_owner()))
        return context(call)

    async def close():
        events.append(("close", asyncio.current_task(), current_owner()))

    ref = await host.open("session", str(tmp_path), bootstrap, close=close)
    caller = asyncio.create_task(host.execute(ref, request()))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    retry = asyncio.create_task(host.execute(ref, request()))
    release.set()
    assert (await retry).result.output == "done"
    assert (await host.execute(ref, request())).result.output == "done"
    assert ledger.get("session", "one")["status"] == "SUCCEEDED"
    await host.close(ref)
    assert [event[0] for event in events] == ["open", "call", "close"]
    assert all(event[1:] == events[0][1:] for event in events)


@pytest.mark.asyncio
async def test_failed_effect_is_persisted_unknown_and_not_retried(tmp_path):
    ledger = EffectLedger(SQLiteThreadStore(tmp_path / "db"))
    host = WorkerResourceHost("worker", ledger=ledger)
    calls = []

    async def call(_arguments):
        calls.append(1)
        raise ConnectionError("response lost after mutation")

    async def bootstrap():
        return context(call)

    ref = await host.open("session", str(tmp_path), bootstrap, close=no_close)
    with pytest.raises(OutcomeUnknown):
        await host.execute(ref, request())
    with pytest.raises(OutcomeUnknown):
        await host.execute(ref, request())
    await host.close(ref)
    assert calls == [1]
    assert host.operation(ref, "one")["status"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_restart_refuses_owner_recreation_and_persists_unknown(tmp_path):
    ledger = EffectLedger(SQLiteThreadStore(tmp_path / "db"))
    host = WorkerResourceHost("worker", ledger=ledger)

    async def bootstrap():
        return context(None)

    ref = await host.open("session", str(tmp_path), bootstrap, close=no_close)
    ledger.begin(
        "session",
        "interrupted",
        str(tmp_path),
        "digest",
        {"resource_owner": asdict(ref)},
    )
    restarted = WorkerResourceHost("worker", ledger=ledger)
    with pytest.raises(ResourceOwnerLost):
        await restarted.open(
            "session", str(tmp_path), bootstrap, close=no_close
        )
    with pytest.raises(ResourceOwnerLost):
        await restarted.execute(ref, request())
    assert restarted.operation(ref, "interrupted")["status"] == "UNKNOWN"
    assert ledger.get("session", "interrupted")["status"] == "UNKNOWN"
    await host.close(ref)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "stream_sse", "stream_http"])
async def test_real_mcp_registry_reuses_live_session_across_effects(
    tmp_path, transport
):
    import json
    import socket
    import sys
    from pathlib import Path

    from gc_core.tools.connect_mcp.agent_tools import (
        aget_mcp_tools_data_for_config,
    )
    from gc_testkit.helpers.tool_ctx import local_ctx

    fixture = Path(__file__).with_name("mcp_fixture_server.py")
    process = None
    if transport == "stdio":
        server = {
            "transport": "stdio",
            "persistent_session": True,
            "command": sys.executable,
            "args": [str(fixture), "stdio", "0"],
        }
    else:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        mode = "sse" if transport == "stream_sse" else "streamable-http"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(fixture),
            mode,
            str(port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(15):
            while True:
                try:
                    _, writer = await asyncio.open_connection(
                        "127.0.0.1", port
                    )
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.05)
        server = {
            "transport": transport,
            "persistent_session": True,
            "url": f"http://127.0.0.1:{port}",
        }
    config = {"launch_root": str(tmp_path), "mcp-servers": {"counter": server}}
    ledger = EffectLedger(SQLiteThreadStore(tmp_path / "db"))
    host = WorkerResourceHost("worker", ledger=ledger)
    registry = {}

    async def bootstrap():
        ctx = local_ctx(config)
        ctx.tools_data, _ = await aget_mcp_tools_data_for_config(
            config, os_backend=ctx.os_backend
        )
        registry.update(ctx.tools_data)
        return ctx

    ref = None
    try:
        ref = await host.open("session", str(tmp_path), bootstrap)
        increment = next(
            name for name in registry if name.endswith("increment")
        )
        arguments = {"delay": 0}

        def effect(operation):
            return ToolEffectRequest(
                operation,
                increment,
                None,
                arguments,
                arguments_digest(arguments),
            )

        first = await host.execute(ref, effect("first"))
        replay = await host.execute(ref, effect("first"))
        second = await host.execute(ref, effect("second"))

        def counter(value):
            payload = json.loads(value.result.output)["result"]
            if isinstance(payload, dict) and "content" in payload:
                return json.loads(payload["content"][0]["text"])
            if isinstance(payload, list):
                return json.loads(payload[0]["text"])
            return json.loads(payload) if isinstance(payload, str) else payload

        one, two = counter(first), counter(second)
        assert replay == first
        assert one["count"] == 1 and two["count"] == 2
        assert one["session"] == two["session"]
        assert one["pid"] == two["pid"]
    finally:
        if ref is not None:
            await host.close(ref)
        if process is not None:
            process.terminate()
            await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_reply", [False, True])
async def test_background_receipt_has_owner_or_unknown_without_repeat(
    tmp_path, monkeypatch, lost_reply
):
    from gc_core.tools.exec.execute_command import ExecuteCommand
    from gc_testkit.helpers.tool_ctx import local_ctx

    ledger = EffectLedger(SQLiteThreadStore(tmp_path / "db"))
    host = WorkerResourceHost("worker", ledger=ledger)
    ctx = local_ctx({"launch_root": str(tmp_path)})
    ctx.tools_data = {
        "exec_command": {
            "tool_fabric": ExecuteCommand,
            "spec": ExecuteCommand.tool_description(),
        }
    }
    calls = []

    async def spawn(_command, **_kwargs):
        calls.append(1)
        if lost_reply:
            raise TimeoutError("spawn may have completed")
        return {"pid": 12345}

    monkeypatch.setattr(ctx.os_backend, "aspawn_background", spawn)

    async def bootstrap():
        return ctx

    ref = await host.open("session", str(tmp_path), bootstrap, close=no_close)
    arguments = {
        "command": "echo background",
        "cwd": str(tmp_path),
        "run_in_background": True,
    }
    effect = ToolEffectRequest(
        "background",
        "exec_command",
        None,
        arguments,
        arguments_digest(arguments),
    )
    try:
        if lost_reply:
            with pytest.raises(OutcomeUnknown):
                await host.execute(ref, effect)
            assert host.operation(ref, "background")["status"] == "UNKNOWN"
        else:
            result = await host.execute(ref, effect)
            process = result.background_processes[0]
            assert process.owner == ref and process.pid == 12345
            assert process.operation_id == "background"
            assert (await host.execute(ref, effect)) == result
        assert calls == [1]
    finally:
        await host.close(ref)


async def test_child_preparation_and_effect_share_owner_task_without_reconnect(
    tmp_path,
):
    host = WorkerResourceHost(
        "worker", ledger=EffectLedger(SQLiteThreadStore(tmp_path / "db"))
    )
    tasks = []
    parent = context(lambda _args: None)

    async def bootstrap():
        tasks.append(asyncio.current_task())
        return parent

    async def call(_args):
        tasks.append(asyncio.current_task())
        return "child registry"

    async def prepare(actual_parent):
        assert actual_parent is parent
        tasks.append(asyncio.current_task())
        return context(call)

    ref = await host.open("owner", str(tmp_path), bootstrap, close=no_close)
    child = await host.prepare_context(ref, "child", prepare)
    assert await host.prepare_context(ref, "child", prepare) is child
    result = await host.execute(
        ref, request("child/effect"), context_id="child"
    )
    assert result.result.output == "child registry"
    with pytest.raises(OutcomeUnknown, match="different effect"):
        await host.execute(ref, request("child/effect"))
    assert len(tasks) == 3
    assert all(task is tasks[0] for task in tasks)
    await host.close(ref)
    with pytest.raises(ResourceOwnerLost):
        await host.prepare_context(ref, "other", prepare)


@pytest.mark.asyncio
async def test_forget_releases_closed_actor_but_retains_owner_tombstone(
    tmp_path,
):
    ledger = EffectLedger(SQLiteThreadStore(tmp_path / "db"))
    host = WorkerResourceHost("worker", ledger=ledger)

    async def bootstrap():
        return context(None)

    async def child(_ctx):
        return context(None)

    ref = await host.open("session", str(tmp_path), bootstrap, close=no_close)
    actor = host._owners["session"]
    await host.prepare_context(ref, "child", child)
    with pytest.raises(RuntimeError, match="must finish"):
        host.forget(ref)
    await host.close(ref)
    assert not actor._contexts and not actor._context_futures
    host.forget(ref)
    host.forget(ref)
    assert not host._owners
    assert ledger.get("session", "__resource_owner__")["status"] == "CLOSED"
    with pytest.raises(ResourceOwnerLost):
        await host.open("session", str(tmp_path), bootstrap, close=no_close)


@pytest.mark.asyncio
async def test_failed_bootstrap_releases_actor_and_fences_restart(tmp_path):
    ledger = EffectLedger(SQLiteThreadStore(tmp_path / "db"))
    host = WorkerResourceHost("worker", ledger=ledger)

    async def bootstrap():
        raise ValueError("discovery failed")

    with pytest.raises(ValueError, match="discovery failed"):
        await host.open("session", str(tmp_path), bootstrap, close=no_close)
    assert not host._owners
    assert (
        ledger.get("session", "__resource_owner__")["status"] == "OWNER_LOST"
    )
    with pytest.raises(ResourceOwnerLost):
        await host.open("session", str(tmp_path), bootstrap, close=no_close)
