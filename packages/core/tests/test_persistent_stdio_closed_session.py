"""Закрытая сессия постоянного stdio не поднимает процесс.

Закрытие может прийти, пока поток обмена ещё запускает процесс: сессию уже
сняли с реестра, а процесса закрытие не видело. Такой процесс не нашёл бы
никто — ни закрытие владельца, ни уборка на выходе. И брошенный вызов не
заводит новой сессии: иначе она появилась бы после закрытия владельца.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest
from gc_core.mcp_session_owner import new_owner_token, use_owner
from gc_core.tools.connect_mcp import persistent_stdio
from gc_core.tools.connect_mcp.abandonable import run_abandonable
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
    PersistentStdioSession,
)
from gc_testkit.mcp import launches, stdio_cfg, wait_for
from gc_testkit.mcp.fixtures import mcp_reaper

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)

_CEILING_SEC = 30.0


def _persistent(control) -> dict:
    return {**stdio_cfg(control), "persistent_session": True}


async def test_closed_session_does_not_start(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    session = PersistentStdioSession(_persistent(control))
    session.close()

    with pytest.raises(ConnectionError):
        session.call("tools/list", {}, 5)

    assert launches(control) == []


async def test_close_during_spawn_kills_the_new_process(
    tmp_path, monkeypatch, mcp_reaper
):
    real = persistent_stdio._spawn_detached_stdio_process
    spawned = []

    def slow_spawn(cfg):
        time.sleep(0.5)  # окно запуска: ``_proc`` ещё пуст
        spawned.append(real(cfg))
        return spawned[-1]

    monkeypatch.setattr(
        persistent_stdio, "_spawn_detached_stdio_process", slow_spawn
    )
    control = tmp_path / "s.json"
    session = PersistentStdioSession(_persistent(control))

    exchange = asyncio.ensure_future(
        asyncio.to_thread(session.call, "tools/list", {}, 10)
    )
    await asyncio.sleep(0.15)
    await asyncio.wait_for(session.aclose(), _CEILING_SEC)

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(exchange, _CEILING_SEC)
    # Процесс гасится сразу после запуска, порой раньше, чем успеет
    # записать журнал, — поэтому смотрим на сам порождённый процесс.
    [proc] = spawned
    await wait_for(lambda: proc.poll() is not None)


async def test_abandoned_call_does_not_create_session(tmp_path):
    entered, release, finished = (threading.Event() for _ in range(3))
    owner = new_owner_token("closed-test")
    registry = PersistentStdioRegistry.instance()

    def body():
        try:
            entered.set()
            release.wait(10)
            registry.get_or_create("srv", _persistent(tmp_path / "s.json"))
        finally:
            finished.set()

    with use_owner(owner):
        call = asyncio.ensure_future(run_abandonable(body))
        await asyncio.to_thread(entered.wait, 10)
        call.cancel()
        await asyncio.wait({call}, timeout=_CEILING_SEC)
    release.set()
    await asyncio.to_thread(finished.wait, 10)

    assert [key for key in registry._sessions if key[0] == owner] == []
