"""Сигнал будит цикл событий главного потока.

Обработчик Python исполняется только на главном потоке, между
инструкциями. Сигнал, принятый другим потоком или пришедший, когда цикл
уже заснул в ожидании событий, сам цикл не будит: обработчик ждёт
следующего пробуждения цикла — сколь угодно долго, если событий нет.
``add_signal_handler`` ставит процессу wakeup-fd цикла: интерпретатор пишет
в него номер любого сигнала с обработчиком Python, и цикл просыпается.

Колбэк цикла пуст: сигнал по-прежнему разбирает обработчик Python, который
вызывающий ставит поверх — после ``wake_loop_on_signal``, потому что
``add_signal_handler`` подменяет обработчик Python своим.
"""

from __future__ import annotations

import asyncio


def wake_loop_on_signal(loop: asyncio.AbstractEventLoop, signum: int) -> bool:
    """Сигнал ``signum`` будит ``loop``; ``True`` — пробуждение поставлено.

    Цикл без поддержки (Windows) или не на главном потоке — ``False``:
    остаётся один обработчик Python. ``ProactorEventLoop`` (умолчание
    Windows) сам ставит wakeup-fd и просыпается по сигналу, селекторный
    цикл Windows — нет.

    Предусловие: у цикла нет своего колбэка на этот сигнал, а у процесса —
    чужого wakeup-fd. Пробуждение молча заменяет и то, и другое, а снятие
    не возвращает ни колбэк, ни прежний wakeup-fd.
    """
    try:
        loop.add_signal_handler(signum, _woken)
    # ``NotImplementedError`` (цикл без поддержки) — подкласс
    # ``RuntimeError``.
    except (RuntimeError, ValueError, OSError):
        return False
    return True


def stop_waking_loop(loop: asyncio.AbstractEventLoop, signum: int) -> None:
    """Снять пробуждение ``wake_loop_on_signal``; сбой цикла — не помеха.

    Сигнал остаётся с диспозицией по умолчанию: прежний обработчик Python
    вызывающий возвращает сам. Wakeup-fd цикл снимает вместе с последним
    своим сигналом — пробуждения других сигналов на этом цикле его
    сохраняют, порядок снятия не важен.
    """
    if loop.is_closed():
        return
    try:
        loop.remove_signal_handler(signum)
    except (RuntimeError, ValueError, OSError):
        pass


def _woken() -> None:
    """Колбэк цикла: цикл проснулся, остальное — у обработчика Python."""
