"""SIGTERM хоста гасит процессы MCP аварийным путём (спека §5.12).

``gc-cli`` и ``gc-acp`` ставят обработчик сразу после создания сервиса и
снимают его на выходе из области сервиса. Штатное закрытие по SIGTERM не
исполняется: ход не останавливается, его вызовы MCP до выхода процесса
получают «сервис MCP остановлен». У TUI SIGTERM слушает сторож терминала,
у сервера SIGTERM — штатная остановка.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from collections.abc import Callable
from typing import Any

from gc_core.tools.connect_mcp.service import McpService
from gc_core.utils.signal_wakeup import stop_waking_loop, wake_loop_on_signal


def kill_mcp_on_sigterm(service: McpService) -> Callable[[], None]:
    """SIGTERM гасит процессы ``service`` и завершает процесс действием
    сигнала по умолчанию; вернуть функцию, снимающую обработчик.

    Обработчик Python первым делом возвращает SIGTERM диспозицию по
    умолчанию — повторный сигнал завершает процесс сразу, второе гашение
    не нужно: первое уже идёт. Затем ``service.kill_processes_now()``
    (синхронно, замков не берёт, ждёт глухих не дольше срока гашения) и
    тот же сигнал себе: процесс умирает от SIGTERM, как без обработчика.
    Цикл, работающий на главном потоке, сигнал будит (wakeup-fd): иначе
    сигнал, принятый другим потоком или пришедший перед сном цикла, ждал
    бы следующего события — ACP ждёт строку stdin сколь угодно.

    Вне главного потока и не на POSIX ничего не ставится — вернётся
    пустая функция. Снятие возвращает прежний обработчик и снимает
    пробуждение цикла.
    """
    if os.name != "posix" or (
        threading.current_thread() is not threading.main_thread()
    ):
        return _keep
    previous = signal.getsignal(signal.SIGTERM)

    def _handler(signum: int, frame: Any) -> None:
        del frame
        signal.signal(signum, signal.SIG_DFL)
        try:
            service.kill_processes_now()
        finally:
            os.kill(os.getpid(), signum)

    loop = _running_loop()
    woken = loop is not None and wake_loop_on_signal(loop, signal.SIGTERM)
    signal.signal(signal.SIGTERM, _handler)

    def restore() -> None:
        if woken:
            stop_waking_loop(loop, signal.SIGTERM)
        signal.signal(
            signal.SIGTERM, signal.SIG_DFL if previous is None else previous
        )

    return restore


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _keep() -> None:
    """Снимать нечего: обработчик не ставился."""
