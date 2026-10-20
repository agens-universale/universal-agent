"""Ctrl+C во время прогона gc-cli — тот же стоп, что у остальных режимов."""

from __future__ import annotations

import signal
import threading
from typing import Any


def install_run_sigint(executor: Any):
    """Первый Ctrl+C снимает ход. Второй — обычное прерывание процесса.

    На вопросе ``ask_human`` перехват вопроса стоит выше и остаётся отказом
    отвечать: он подменяет этот обработчик на время вопроса.
    """
    if threading.current_thread() is not threading.main_thread():
        return None
    try:
        previous = signal.getsignal(signal.SIGINT)
    except (ValueError, OSError):
        return None
    hits = {"n": 0}

    def _handler(signum, frame):
        del signum, frame
        hits["n"] += 1
        if hits["n"] > 1:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        executor.stop_run()

    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):
        return None
    return previous


def restore_run_sigint(previous: Any) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGINT, previous)
    except (ValueError, OSError, TypeError):
        pass
