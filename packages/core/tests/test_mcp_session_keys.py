"""Ключ сессии из определения сервера и закрытие сессии по ключу.

Потребитель сервиса держит свою сессию сервера под ключом из поля
``SESSION_KEY_FIELD`` определения: разные ключи — разные сессии даже у
одинаковых определений, а закрыть можно ровно одну. Без поля ключ прежний.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from gc_core.mcp_session_owner import use_owner
from gc_core.tools.connect_mcp import client, stream_client
from gc_core.tools.connect_mcp import persistent_stdio as ps
from gc_core.tools.connect_mcp.server_identity import SESSION_KEY_FIELD
from gc_testkit.mcp import (
    alive,
    call_tool,
    exits,
    launches,
    set_behavior,
    stdio_cfg,
    stream_server,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_owner, mcp_reaper

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)


def _keyed(cfg: dict, key: str) -> dict:
    return {**cfg, "persistent_session": True, SESSION_KEY_FIELD: key}


async def _echo_pid(cfg: dict) -> int:
    out = await call_tool("srv", cfg, "echo", {"text": "x"})
    return out["pid"]


async def test_different_keys_get_separate_processes(
    tmp_path, mcp_reaper, mcp_owner
):
    cfg = stdio_cfg(tmp_path / "s.json")

    first = await _echo_pid(_keyed(cfg, "srv@1"))
    second = await _echo_pid(_keyed(cfg, "srv@2"))
    third = await _echo_pid(_keyed(cfg, "srv@1"))

    assert first == third
    assert second != first


async def test_aclose_one_closes_only_that_session(
    tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    cfg = stdio_cfg(control)
    first = await _echo_pid(_keyed(cfg, "srv@1"))
    second = await _echo_pid(_keyed(cfg, "srv@2"))
    registry = ps.PersistentStdioRegistry.instance()

    assert await registry.aclose_one("srv@1") is True
    assert await registry.aclose_one("srv@1") is False
    assert first in exits(control)
    assert alive(second)
    assert second not in exits(control)


async def test_aclose_one_finishes_close_when_caller_is_cancelled(
    tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    control = tmp_path / "s.json"
    # По концу stdin сервер не выходит: штатный выход только по SIGTERM,
    # то есть только если закрытие дошло до терминации.
    set_behavior(control, ignore_eof=True)
    pid = await _echo_pid(_keyed(stdio_cfg(control), "srv@1"))
    entered = asyncio.Event()
    release = asyncio.Event()
    terminate = ps._aterminate_process_tree

    async def _gated(proc, graceful_timeout=5.0):
        entered.set()
        await release.wait()
        await terminate(proc, graceful_timeout=graceful_timeout)

    monkeypatch.setattr(ps, "_aterminate_process_tree", _gated)
    registry = ps.PersistentStdioRegistry.instance()
    try:
        closing = asyncio.create_task(registry.aclose_one("srv@1"))
        await asyncio.wait_for(entered.wait(), 5)

        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
    finally:
        release.set()

    await wait_for(lambda: pid in exits(control))
    assert await registry.aclose_one("srv@1") is False


async def test_stream_sessions_same_url_different_keys(
    tmp_path, mcp_reaper, mcp_owner
):
    async with stream_server(tmp_path, "stream_http") as (cfg, _control):
        k1 = _keyed(cfg, "k1")
        first = await call_tool("st", k1, "action", {})
        second = await call_tool("st", _keyed(cfg, "k2"), "action", {})
        again = await call_tool("st", k1, "action", {})
        # Закрытие по ключу — существующее снятие сессии менеджером; на
        # живом сервере, а не после его гашения.
        await stream_client._SESSION_MANAGER.ainvalidate("k1")
        await stream_client._SESSION_MANAGER.ainvalidate("k2")

    assert first["session"] != second["session"]
    assert first["session"] == again["session"]


def test_forget_owner_sessions_drops_only_own(monkeypatch):
    sessions = {
        ("o1", "a"): "sid-1",
        ("o1", "b"): "sid-2",
        ("o2", "a"): "sid-3",
    }
    monkeypatch.setattr(client, "_MCP_SESSIONS", sessions)

    with use_owner("o1"):
        client.forget_owner_sessions()

    assert sessions == {("o2", "a"): "sid-3"}


async def test_without_key_field_behaviour_is_unchanged(
    tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    cfg = {**stdio_cfg(control), "persistent_session": True}

    first = await call_tool("p", cfg, "echo", {"text": "x"})
    second = await call_tool("p", cfg, "echo", {"text": "y"})

    sessions = ps.PersistentStdioRegistry.instance()._sessions
    assert first["pid"] == second["pid"]
    assert launches(control) == [first["pid"]]
    assert (mcp_owner, "p") in sessions
