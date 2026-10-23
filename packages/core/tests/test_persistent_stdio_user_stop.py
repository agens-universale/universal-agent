"""Стоп долгоживущего stdio шлёт notifications/cancelled и не берёт поздний ответ.

Процесс сервера остаётся. Ответ снятого id не становится результатом ни этого
вызова, ни следующего: очередь общая, чужой id при чтении пропускается.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from gc_core.mcp_session_owner import current_owner
from gc_core.tools.connect_mcp.client_port import AgentRuntimeClient
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
    PersistentStdioSession,
)
from gc_core.tools.connect_mcp.transport import forget_stdio_reader
from gc_core.user_stop import (
    UserStop,
    UserStopInterrupt,
    bind_tool_call,
    bind_user_stop,
)

_SERVER = r"""
import json, sys

marker = sys.argv[1]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    method = req.get("method")
    if method == "notifications/cancelled":
        with open(marker, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(req) + "\n")
        rid = (req.get("params") or {}).get("requestId")
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": rid, "result": {"late": True},
        }) + "\n")
        sys.stdout.flush()
        continue
    if method == "notifications/initialized":
        continue
    rid = req.get("id")
    if rid is None:
        continue
    name = ((req.get("params") or {}).get("name")) or ""
    if name == "slow":
        continue
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0", "id": rid, "result": {"ok": name or method},
    }) + "\n")
    sys.stdout.flush()
"""


class _Log:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def info(self, msg, *args, extra=None) -> None:
        del msg, args
        self.records.append(dict(extra or {}))

    def events(self, name: str) -> list[dict]:
        return [r for r in self.records if r.get("event") == name]


def _cfg(script: str) -> dict:
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [script],
        "persistent_session": True,
    }


async def _wait_inflight(client: AgentRuntimeClient):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        session = getattr(client, "_stdio_session", None)
        if session is not None and session._inflight_id is not None:
            return session
        await asyncio.sleep(0.02)
    raise AssertionError("stdio request was not started")


async def test_stop_cancels_the_stdio_request_and_ignores_its_late_result(
    tmp_path,
):
    script = tmp_path / "server.py"
    script.write_text(_SERVER, encoding="utf-8")
    marker = tmp_path / "cancelled.jsonl"
    cfg = _cfg(str(script))
    cfg["args"] = [str(script), str(marker)]
    client = AgentRuntimeClient("stop-stdio", cfg)
    log = _Log()
    gate = UserStop()
    gate.prepare_run(asyncio.get_running_loop(), log)
    reg = PersistentStdioRegistry.instance()
    key = (current_owner(), "stop-stdio")

    async def _work():
        with bind_user_stop(gate):
            with bind_tool_call("slow", "call-1"):
                return await client.acall_tool("slow", {}, timeout=10)

    task = asyncio.create_task(_work())
    try:
        session = await _wait_inflight(client)
        req_id = session._inflight_id
        t0 = time.monotonic()
        gate.request(log)
        with pytest.raises(UserStopInterrupt):
            await task
        assert time.monotonic() - t0 < 3
        assert session._is_alive()

        deadline = time.monotonic() + 2
        notes: list[dict] = []
        while time.monotonic() < deadline:
            if marker.exists():
                notes = [
                    json.loads(line)
                    for line in marker.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if notes:
                    break
            await asyncio.sleep(0.02)
        assert len(notes) == 1
        assert notes[0]["method"] == "notifications/cancelled"
        assert "id" not in notes[0]
        assert notes[0]["params"]["requestId"] == req_id
        assert notes[0]["params"]["reason"] == "User requested cancellation"
        killed = log.events("user_stop_killed")
        assert len(killed) == 1
        assert killed[0]["kind"] == "mcp"
        assert log.events("user_stop_unkillable") == []

        second = await client.acall_tool("second", {}, timeout=5)
        assert second["result"]["ok"] == "second"
        assert second["id"] != req_id
        assert second["result"].get("late") is not True
        assert session._is_alive()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        session = reg._sessions.get(key)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.aclose()
        with reg._lock:
            reg._sessions.pop(key, None)


def test_cancel_inflight_writes_one_notification():
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(write_fd, "w")
    session = PersistentStdioSession(_cfg("python"))
    session._inflight_id = 7
    session._proc = SimpleNamespace(
        stdin=stdin, stdout=None, poll=lambda: None
    )
    try:
        assert session.cancel_inflight() is True
        assert session.cancel_inflight() is True
        raw = os.read(read_fd, 4096)
        os.set_blocking(read_fd, False)
        try:
            extra = os.read(read_fd, 4096)
        except BlockingIOError:
            extra = b""
        assert extra == b""
        note = json.loads(raw.decode())
        assert note["method"] == "notifications/cancelled"
        assert note["params"]["requestId"] == 7
        assert "id" not in note
    finally:
        stdin.close()
        os.close(read_fd)


def test_cancel_without_a_request_does_not_write():
    session = PersistentStdioSession(_cfg("python"))
    assert session.cancel_inflight() is None
    session._inflight_id = 1
    assert session.cancel_inflight() is None


def test_cancel_with_a_dead_stdin_is_not_signalled():
    session = PersistentStdioSession(_cfg("python"))
    session._inflight_id = 4
    session._proc = SimpleNamespace(stdin=None, stdout=None, poll=lambda: None)
    assert session.cancel_inflight() is False


def test_ignored_id_is_not_returned_as_the_result():
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    session = PersistentStdioSession(_cfg("python"))
    session._proc = SimpleNamespace(
        stdout=stream, stdin=None, poll=lambda: None
    )
    session._ignored_ids.add(2)
    line = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"late": True}})
    try:
        os.write(write_fd, (line + "\n").encode())
        os.close(write_fd)
        write_fd = -1
        got = session._read_matching(2, timeout=2)
        assert got == {}
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        stream.close()
        forget_stdio_reader(stream)
