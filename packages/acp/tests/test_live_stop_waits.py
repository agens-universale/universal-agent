"""Ответ отмены не уходит, пока идущая работа не снята."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gc_acp.facade import _await_live_stop
from gc_core.user_stop import UserStop, bind_user_stop


class _Log:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def info(self, msg, *args, extra=None) -> None:
        del msg, args
        self.records.append(dict(extra or {}))


def _stop(log: _Log | None = None) -> UserStop:
    gate = UserStop()
    gate.prepare_run(asyncio.get_running_loop(), log)
    return gate


async def test_acp_stop_waits_until_settled():
    log = _Log()
    gate = _stop(log)
    held = asyncio.Event()

    class _Live:
        agent = SimpleNamespace(_executor=SimpleNamespace(user_stop=gate))

    async def _hold() -> None:
        async def _release() -> None:
            held.set()

        with bind_user_stop(gate):
            async with gate.arm(kind="exec", release=_release):
                await held.wait()

    held_task = asyncio.create_task(_hold())
    await asyncio.sleep(0.05)

    async def _cancel() -> None:
        gate.request(log)
        await _await_live_stop(_Live())

    await _cancel()
    assert held.is_set()
    await held_task
