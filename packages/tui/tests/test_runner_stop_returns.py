"""Esc не ждёт снятие: кадр уходит, команда умирает на потоке воркера."""

from __future__ import annotations

import asyncio
import threading
import time

from gc_core.user_stop import UserStop, bind_user_stop
from gc_tui.services.agent_runner import AgentRunner


class _Log:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def info(self, msg, *args, extra=None) -> None:
        del msg, args
        self.records.append(dict(extra or {}))

    def events(self, name: str) -> list[dict]:
        return [r for r in self.records if r.get("event") == name]


def test_tui_stop_returns_before_the_worker_settles():
    log = _Log()
    loop = asyncio.new_event_loop()
    gate = UserStop()
    release_done = threading.Event()

    class _Executor:
        user_stop = gate

        def stop_run(self) -> None:
            gate.request(log)

    async def _arm() -> None:
        gate.prepare_run(asyncio.get_running_loop(), log)
        finish = asyncio.Event()

        async def _release() -> None:
            await asyncio.sleep(0.3)
            release_done.set()
            finish.set()

        with bind_user_stop(gate):
            async with gate.arm(kind="exec", release=_release):
                await finish.wait()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_arm())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    for _ in range(50):
        if gate._stack:
            break
        time.sleep(0.02)
    assert gate._stack
    runner = AgentRunner(_Executor())
    started = time.monotonic()
    runner.stop()
    assert time.monotonic() - started < 0.1
    assert not release_done.is_set()
    thread.join(5)
    assert not thread.is_alive()
    assert release_done.is_set()
    assert log.events("user_stop_killed")
    loop.close()
