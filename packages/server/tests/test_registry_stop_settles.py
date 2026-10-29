"""Стоп реестра отвечает, когда работа снята, и не считает таймаут успехом."""

from __future__ import annotations

import asyncio
import time

import pytest
from gc_core.backends.local import LocalFsBackend
from gc_core.user_stop import UserStop, bind_user_stop
from gc_server.webhook.registry import ActiveRun, AgentRunRegistry


class _Log:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def info(self, msg, *args, extra=None) -> None:
        del msg, args
        self.records.append(dict(extra or {}))

    def events(self, name: str) -> list[dict]:
        return [r for r in self.records if r.get("event") == name]


def _stop(log: _Log | None = None) -> UserStop:
    gate = UserStop()
    gate.prepare_run(asyncio.get_running_loop(), log)
    return gate


async def test_server_stop_returns_when_the_command_is_dead():
    log = _Log()
    gate = _stop(log)
    dead = asyncio.Event()

    class _Executor:
        user_stop = gate

        def stop_run(self) -> None:
            gate.request(log)

    registry = AgentRunRegistry()
    registry._runs["sys:task"] = ActiveRun(
        key="sys:task",
        system="sys",
        task_id="task",
        executor=_Executor(),
        started_at=time.time(),
    )

    async def _work():
        with bind_user_stop(gate):
            result = await LocalFsBackend().aexec("sleep 30", timeout=30)
            dead.set()
            return result

    task = asyncio.create_task(_work())
    await asyncio.sleep(0.3)
    yielded = False

    async def _other() -> None:
        nonlocal yielded
        yielded = True

    other = asyncio.create_task(_other())
    assert await registry.stop(system="sys", task_id="task") is True
    assert yielded is True
    await other
    result = await task
    assert dead.is_set()
    assert result.stopped_by_user is True
    assert log.events("user_stop_killed")


async def test_registry_stop_timeout_is_not_success(monkeypatch):
    """Потолок ожидания не выглядит как снятый прогон."""
    monkeypatch.setattr("gc_core.user_stop._SETTLE_SEC", 0.05)
    gate = UserStop()
    gate._settled.clear()

    class _Executor:
        user_stop = gate

        def stop_run(self) -> None:
            return None

    registry = AgentRunRegistry()
    registry._runs["sys:task"] = ActiveRun(
        key="sys:task",
        system="sys",
        task_id="task",
        executor=_Executor(),
        started_at=time.time(),
    )
    with pytest.raises(TimeoutError):
        await registry.stop(system="sys", task_id="task")
