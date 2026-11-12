"""Ctrl+C ставит стоп хода на цикл и не берёт замок из обработчика сигнала."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import threading

import pytest
from gc_cli.run_stop import (
    _Installed,
    install_run_sigint,
    restore_run_sigint,
)
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


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"), reason="маски сигналов POSIX"
)
def test_press_wakes_a_loop_asleep_without_events():
    """Нажатие, которое принял не главный поток, пока цикл спит без
    событий, снимает ход сразу, а не при следующем пробуждении цикла.

    Главный поток сигнал блокирует, поэтому его принимает поток-нажиматель:
    системный вызов ожидания цикла сигналом не прерывается. Так же выглядит
    сигнал, пришедший между последней проверкой цикла и входом в ожидание.
    """
    loop = asyncio.new_event_loop()
    stopped_after: list[float] = []
    asleep = threading.Event()

    async def _body() -> None:
        started = loop.time()
        stopped = asyncio.Event()

        class _Executor:
            def stop_run(self) -> None:
                stopped_after.append(loop.time() - started)
                stopped.set()

        def _press() -> None:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
            asleep.wait(5)
            signal.pthread_kill(threading.get_ident(), signal.SIGINT)

        previous = install_run_sigint(_Executor())
        assert previous is not None
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            presser = threading.Thread(target=_press)
            presser.start()
            # Флаг ставит сам цикл перед сном: дальше событий у него нет.
            loop.call_soon(asleep.set)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopped.wait(), 3)
        finally:
            presser.join(5)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
            restore_run_sigint(previous)

    try:
        loop.run_until_complete(_body())
    finally:
        loop.close()
    assert stopped_after and stopped_after[0] < 1.0


def test_restore_returns_previous_handler_when_loop_refuses():
    """Сбой снятия пробуждения у цикла не мешает вернуть прежний
    обработчик."""

    class _Loop:
        def is_closed(self) -> bool:
            return False

        def remove_signal_handler(self, _sig) -> bool:
            raise RuntimeError("цикл не снял обработчик")

    def previous(signum, frame) -> None:
        del signum, frame

    before = signal.getsignal(signal.SIGINT)
    try:
        restore_run_sigint(_Installed(previous, _Loop()))
        assert signal.getsignal(signal.SIGINT) is previous
    finally:
        signal.signal(signal.SIGINT, before)
