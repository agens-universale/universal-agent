"""Native resource owner lifecycle: one task owns live MCP/background resources."""

import asyncio

import pytest
from gc_core.execution.contracts import (
    ExecutionFailure,
    ToolEffectRequest,
    ToolOutcomeUnknown,
)
from gc_core.execution.resource_contracts import ResourceOwnerLost
from gc_core.execution.resource_host import WorkerResourceHost
from gc_core.execution.tool_effects import arguments_digest
from gc_core.mcp_session_owner import current_owner
from gc_testkit.settings import fake_ctx


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

    return fake_ctx(
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
    host = WorkerResourceHost("worker")
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
    await host.close(ref)
    assert [event[0] for event in events] == ["open", "call", "close"]
    assert all(event[1:] == events[0][1:] for event in events)


@pytest.mark.asyncio
async def test_failed_call_is_tool_feedback_and_owner_stays_alive(tmp_path):
    host = WorkerResourceHost("worker")
    calls = []

    async def call(_arguments):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("server rejected the request")
        return "ok"

    async def bootstrap():
        return context(call)

    ref = await host.open("session", str(tmp_path), bootstrap, close=no_close)
    with pytest.raises(ConnectionError):
        await host.execute(ref, request("bad"))
    assert (await host.execute(ref, request("good"))).result.output == "ok"
    await host.close(ref)
    assert calls == [1, 1]


@pytest.mark.asyncio
async def test_restart_loses_owner_as_recoverable_tool_feedback(tmp_path):
    host = WorkerResourceHost("worker")

    async def bootstrap():
        return context(None)

    ref = await host.open("session", str(tmp_path), bootstrap, close=no_close)
    restarted = WorkerResourceHost("worker")
    with pytest.raises(ResourceOwnerLost) as lost:
        await restarted.execute(ref, request())
    assert isinstance(lost.value, ToolOutcomeUnknown)
    assert not isinstance(lost.value, ExecutionFailure)
    fresh = await restarted.open(
        "session", str(tmp_path), bootstrap, close=no_close
    )
    assert fresh.process_epoch != ref.process_epoch
    await restarted.close(fresh)
    await host.close(ref)


# Дольше этого шага тест не ждёт. Иначе застрявшие дискавери, call_tool
# SDK-сессии и process.wait держат весь прогон: у них своего потолка нет.
_MCP_STEP_SEC = 20


async def _await_step(awaitable, seconds=_MCP_STEP_SEC):
    return await asyncio.wait_for(awaitable, seconds)


async def _stop_mcp_fixture(process):
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), 5)


async def _close_host(host, ref):
    if ref is None:
        return
    try:
        await asyncio.wait_for(host.close(ref), _MCP_STEP_SEC)
    except TimeoutError:
        owner = host._owners.get(ref.owner_id)
        task = None if owner is None else owner._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=5)
        raise


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
    config = {
        "launch_root": str(tmp_path),
        # Иначе stdio-чтение сидит до request_timeout_sec (600 с).
        "mcp.meta.request_timeout_sec": 15,
        "mcp.servers": {"counter": server},
    }
    host = WorkerResourceHost("worker")
    registry = {}

    async def bootstrap():
        ctx = local_ctx(config)
        ctx.tools_data, _ = await aget_mcp_tools_data_for_config(
            ctx, os_backend=ctx.os_backend
        )
        registry.update(ctx.tools_data)
        return ctx

    ref = None
    try:
        ref = await _await_step(host.open("session", str(tmp_path), bootstrap))
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

        first = await _await_step(host.execute(ref, effect("first")))
        replay = await _await_step(host.execute(ref, effect("first")))
        second = await _await_step(host.execute(ref, effect("second")))

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
        # open мог создать владельца и не успеть вернуть ref.
        if ref is None and "session" in host._owners:
            ref = host._owners["session"].ref
        try:
            await _close_host(host, ref)
        finally:
            await _stop_mcp_fixture(process)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_reply", [False, True])
async def test_background_receipt_has_owner_or_unknown_without_repeat(
    tmp_path, monkeypatch, lost_reply
):
    from gc_core.tools.exec.execute_command import ExecuteCommand
    from gc_testkit.helpers.tool_ctx import local_ctx

    host = WorkerResourceHost("worker")
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
            with pytest.raises(ToolOutcomeUnknown):
                await host.execute(ref, effect)
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
    host = WorkerResourceHost("worker")
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
    with pytest.raises(ValueError, match="different effect"):
        await host.execute(ref, request("child/effect"))
    assert len(tasks) == 3
    assert all(task is tasks[0] for task in tasks)
    await host.close(ref)
    with pytest.raises(ResourceOwnerLost):
        await host.prepare_context(ref, "other", prepare)


@pytest.mark.asyncio
async def test_forget_releases_closed_actor_and_its_results(
    tmp_path,
):
    host = WorkerResourceHost("worker")

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


@pytest.mark.asyncio
async def test_failed_bootstrap_releases_actor(tmp_path):
    host = WorkerResourceHost("worker")

    async def bootstrap():
        raise ValueError("discovery failed")

    with pytest.raises(ValueError, match="discovery failed"):
        await host.open("session", str(tmp_path), bootstrap, close=no_close)
    assert not host._owners
