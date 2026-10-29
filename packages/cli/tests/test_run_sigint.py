"""Ctrl+C ставит стоп хода на цикл и не берёт замок из обработчика сигнала."""

from __future__ import annotations

import asyncio
import signal
import threading

import pytest
from gc_cli.run_stop import install_run_sigint, restore_run_sigint
from gc_core.utils.sigint_waits import intercept_sigint


def test_cli_sigint_stops_the_run_and_ask_human_stays_a_refusal():
    calls = {"stop": 0}
    held = threading.Lock()

    class _Executor:
        def stop_run(self) -> None:
            with held:
                calls["stop"] += 1

    loop = asyncio.new_event_loop()

    async def _body() -> None:
        previous = install_run_sigint(_Executor())
        try:
            refused = {"n": 0}
            with intercept_sigint(lambda: refused.__setitem__("n", 1)):
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            assert calls["stop"] == 0
            assert refused["n"] == 1
            held.acquire()
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            assert calls["stop"] == 0
            held.release()
            await asyncio.sleep(0)
            assert calls["stop"] == 1
            with pytest.raises(KeyboardInterrupt):
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        finally:
            restore_run_sigint(previous)

    try:
        loop.run_until_complete(_body())
    finally:
        loop.close()
