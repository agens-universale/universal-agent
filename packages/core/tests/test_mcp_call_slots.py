"""Набор слотов блокирующего вызова задаётся на время вызова.

Общий пул процесса — восемь слотов. Потребитель сервиса MCP даёт каждому окну
свой набор, чтобы зависшие вызовы одного окна не занимали слоты другого.
"""

import asyncio
import threading

from gc_core.tools.connect_mcp import abandonable
from gc_core.tools.connect_mcp.abandonable import run_abandonable

_CEILING = 5


async def _occupy_shared_pool(gate: threading.Event) -> list[asyncio.Task]:
    """Занять все слоты общего пула и дождаться, что каждый реально занят."""
    entered = [threading.Event() for _ in range(abandonable._MAX_WORKERS)]

    def _hold(entry: threading.Event) -> None:
        entry.set()
        gate.wait(_CEILING * 2)

    holders = [
        asyncio.ensure_future(run_abandonable(_hold, entry))
        for entry in entered
    ]
    for entry in entered:
        assert await asyncio.wait_for(
            asyncio.to_thread(entry.wait, _CEILING), _CEILING + 1
        ), "держатель не занял слот общего пула"
    return holders


async def _release(gate: threading.Event, holders: list[asyncio.Task]) -> None:
    gate.set()
    await asyncio.wait_for(asyncio.gather(*holders), _CEILING)


async def test_own_slots_do_not_wait_for_shared_pool():
    gate = threading.Event()
    holders = await _occupy_shared_pool(gate)
    try:
        with abandonable.use_call_slots(threading.BoundedSemaphore(1)):
            result = await asyncio.wait_for(run_abandonable(lambda: 7), 2)
        assert result == 7
    finally:
        await _release(gate, holders)


async def test_without_slots_shared_pool_is_used():
    gate = threading.Event()
    holders = await _occupy_shared_pool(gate)
    try:
        ninth = asyncio.ensure_future(run_abandonable(lambda: 7))
        done, _ = await asyncio.wait({ninth}, timeout=0.3)
        assert not done, "девятый вызов обошёл занятый общий пул"
        gate.set()
        assert await asyncio.wait_for(ninth, _CEILING) == 7
    finally:
        await _release(gate, holders)
