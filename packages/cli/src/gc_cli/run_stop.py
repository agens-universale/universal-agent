"""Ctrl+C во время прогона gc-cli — тот же стоп, что у остальных режимов."""

from __future__ import annotations

import asyncio
import signal
import threading
from typing import Any, NamedTuple

from gc_core.utils.signal_wakeup import stop_waking_loop, wake_loop_on_signal


class _Installed(NamedTuple):
    previous: Any
    #: Цикл, которому поставлено пробуждение по сигналу; ``None`` — не
    #: поставлено (цикл без ``add_signal_handler``, как на Windows).
    woken: asyncio.AbstractEventLoop | None


def install_run_sigint(executor: Any):
    """Первый Ctrl+C снимает ход. Второй — обычное прерывание процесса.

    На вопросе ``ask_human`` перехват вопроса стоит выше и остаётся отказом
    отвечать: он подменяет этот обработчик на время вопроса.

    Из сигнала только очередь на цикл. ``stop_run`` берёт замок исполнителя,
    и вызов из обработчика на том же потоке встал бы навсегда, если замок
    уже держит ход.

    Обработчик Python исполняется только на главном потоке между
    инструкциями: сигнал, пришедший, когда цикл уже заснул в ожидании
    событий (или принятый другим потоком), сам цикл не будит, и нажатие
    ждало бы следующего пробуждения — в закрытии MCP весь бюджет. Поэтому
    сигнал ещё и будит цикл (``wake_loop_on_signal``), а решает по-прежнему
    обработчик ниже: перехват вопроса его подменяет, и нажатие на вопросе
    стоп хода не задевает.
    """
    if threading.current_thread() is not threading.main_thread():
        return None
    try:
        previous = signal.getsignal(signal.SIGINT)
        loop = asyncio.get_running_loop()
    except (ValueError, OSError, RuntimeError):
        return None
    hits = {"n": 0}

    def _handler(signum, frame):
        del signum, frame
        hits["n"] += 1
        if hits["n"] > 1:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        loop.call_soon_threadsafe(executor.stop_run)

    woken = loop if wake_loop_on_signal(loop, signal.SIGINT) else None
    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):
        restore_run_sigint(_Installed(previous, woken))
        return None
    return _Installed(previous, woken)


def restore_run_sigint(installed: Any) -> None:
    if installed is None:
        return
    if installed.woken is not None:
        stop_waking_loop(installed.woken, signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, installed.previous)
    except (ValueError, OSError, TypeError):
        pass
