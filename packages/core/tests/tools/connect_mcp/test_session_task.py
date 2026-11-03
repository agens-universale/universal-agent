"""Два живых MCP: ошибка/отмена A не закрывает B и не отменяет вызывающего."""

import asyncio
import contextlib
import json
import socket
import subprocess
import sys
from contextvars import ContextVar
from pathlib import Path

import pytest
from gc_core.mcp_session_owner import use_owner
from gc_core.tools.connect_mcp import stream_client as sc
from gc_core.tools.connect_mcp.durable_scope import _PROGRESS
from gc_core.tools.connect_mcp.session_task import SessionTask
from mcp.shared.exceptions import McpError

SERVER = Path(__file__).with_name("session_server.py")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(params=["stdio", "streamable-http", "sse"])
async def servers(request, tmp_path, monkeypatch):
    manager = sc._PersistentSessionManager()
    monkeypatch.setattr(sc, "_SESSION_MANAGER", manager)
    processes, logs, configs = [], [], []
    for name in ("a", "b"):
        port = free_port()
        audit = tmp_path / f"{name}.jsonl"
        args = [str(SERVER), request.param, str(port), str(audit)]
        config = {"persistent_session": True}
        if request.param == "stdio":
            config.update(command=sys.executable, args=args)
        else:
            log = (tmp_path / f"{name}.log").open("w")
            logs.append(log)
            processes.append(
                subprocess.Popen(
                    [sys.executable, *args], stdout=log, stderr=log
                )
            )
            config["url"] = f"http://127.0.0.1:{port}"
            async with asyncio.timeout(10):
                while True:
                    try:
                        reader, writer = await asyncio.open_connection(
                            "127.0.0.1", port
                        )
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(0.02)
        configs.append((config, audit))

    async def call(index, **args):
        config, _ = configs[index]
        if request.param == "stdio":
            response = await sc.acall_tool_stream_stdio(
                config, 5, "action", args
            )
        else:
            fn = (
                sc.acall_tool_stream_http
                if request.param == "streamable-http"
                else sc.acall_tool_stream_sse
            )
            response = await fn(config["url"], config, None, 5, "action", args)
        return json.loads(response["content"][0]["text"])

    try:
        yield manager, call, configs
    finally:
        await manager.aclose_all()
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        for log in logs:
            log.close()


async def test_rpc_error_does_not_cancel_turn_or_repeat_tool(servers):
    manager, call, configs = servers
    a = await call(0)
    b = await call(1)
    with pytest.raises(McpError, match="deliberate error"):
        await call(0, fail=True)
    next_b = await call(1)
    assert next_b == dict(b, count=2)
    assert await call(0) == dict(a, count=2)
    errors = [
        json.loads(line) for line in configs[0][1].read_text().splitlines()
    ]
    assert sum(row["event"] == "rpc-error" for row in errors) == 1
    await manager.aclose_all()
    assert not manager._sessions and not manager._workers


@pytest.mark.parametrize("timeout", [False, True])
async def test_cancelled_call_closes_a_but_keeps_b(servers, timeout):
    manager, call, configs = servers
    await call(0)
    b = await call(1)
    pending = asyncio.create_task(call(0, delay=30))
    async with asyncio.timeout(5):
        while configs[0][1].read_text().count('"call"') < 2:
            await asyncio.sleep(0.01)
    if timeout:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(pending, 0.01)
    else:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 10)
    assert await call(1) == dict(b, count=2)
    assert len(manager._workers) == 1


async def test_independent_agents_and_per_call_progress(servers):
    manager, call, _ = servers
    progress = []

    async def first(*_args):
        progress.append("first")

    async def second(*_args):
        progress.append("second")

    with use_owner("one"):
        token = _PROGRESS.set(first)
        try:
            a = await call(0)
        finally:
            _PROGRESS.reset(token)
    with use_owner("two"):
        b = await call(0)
    with use_owner("one"):
        token = _PROGRESS.set(second)
        try:
            assert await call(0) == dict(a, count=2)
        finally:
            _PROGRESS.reset(token)
        await manager.aclose_all()
    with use_owner("two"):
        assert await call(0) == dict(b, count=2)
        await manager.aclose_all()
    assert progress == ["first", "second"]
    assert not manager._sessions and not manager._workers


async def test_queued_cancellation_does_not_cancel_active_call():
    release = asyncio.Event()
    started = asyncio.Event()
    closed = []
    marker = ContextVar("test-session-call", default="default")

    async def close():
        closed.append(asyncio.current_task())

    async def block():
        started.set()
        await release.wait()
        return marker.get()

    worker = SessionTask(None, close)
    token = marker.set("first")
    pending = asyncio.create_task(worker.call(block))
    marker.reset(token)
    await started.wait()
    cancelled = asyncio.create_task(worker.call(block))
    await asyncio.sleep(0)
    cancelled.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await cancelled
    release.set()
    assert await pending == "first"
    assert await worker.call(block) == "default"
    await worker.close()
    assert closed == [worker.task]
