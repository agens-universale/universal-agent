"""Дренаж записей трассы субагента: ни одной потерянной молча.

Колбэк `enqueue` приходит из ЧУЖОГО потока (его дёргает `AgentExecutor.add_message`,
которую зовёт TUI), поэтому запись трассы уезжает на loop прогона через
`run_coroutine_threadsafe`. Планировать и забыть нельзя: `asyncio.run`
вызывающего снимает незавершённые задачи ОТМЕНОЙ, и запись, поставленную
последним сообщением, теряло молча.

Здесь два сценария, ровно те, от которых дренаж и вводился: запись, пришедшая
ВО ВРЕМЯ дренажа, и отмена прогона посреди него.
"""

import asyncio
import logging
import threading

import pytest
from gc_core.tools.subagents.runner import _drain_trace_writes


def _future_for(coro, loop) -> "asyncio.Future":
    return asyncio.run_coroutine_threadsafe(coro, loop)


async def test_write_queued_during_the_drain_is_awaited_too():
    """Снимка мало: пока дренаж ждёт первую запись, приходит вторая.

    Первая редакция снимала список ОДИН раз — future, добавленный после
    снимка, не ждал никто, и именно такая запись (последнее сообщение
    пользователя) терялась.
    """
    loop = asyncio.get_running_loop()
    done: list[str] = []
    futures: list = []
    lock = threading.Lock()

    async def _second():
        done.append("second")

    async def _first():
        done.append("first")
        # Чужой поток добавляет запись ровно в этот момент.
        with lock:
            futures.append(_future_for(_second(), loop))

    with lock:
        futures.append(_future_for(_first(), loop))

    await _drain_trace_writes(futures, lock)

    assert done == ["first", "second"], f"запись потеряна: {done}"
    assert futures == [], "очередь не опустошена"


async def test_cancellation_reports_the_unconfirmed_count(caplog):
    """Отмена посреди дренажа обязана оставить след ЧИСЛОМ, а не тишину.

    Гарантировать завершение тут нечем: после отмены каждое ожидание бросает
    сразу. Поэтому инвариант — не «всё записано», а «известно, сколько не
    подтверждено».
    """
    loop = asyncio.get_running_loop()
    futures: list = []
    lock = threading.Lock()
    started = asyncio.Event()

    async def _slow():
        started.set()
        await asyncio.sleep(5)

    for _ in range(3):
        with lock:
            futures.append(_future_for(_slow(), loop))

    task = asyncio.create_task(_drain_trace_writes(futures, lock))
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()

    with caplog.at_level(logging.WARNING, logger="gc_core.subagents"):
        with pytest.raises(asyncio.CancelledError):
            await task

    texts = [r.getMessage() for r in caplog.records]
    assert any("subagent_trace_writes_unconfirmed" in t for t in texts), f"отмена прошла молча: {texts}"
    assert any("left=3" in t for t in texts), f"неверный остаток: {texts}"


async def test_failed_write_does_not_stop_the_rest():
    """Отказ одной записи логируется и не уносит остальные."""
    loop = asyncio.get_running_loop()
    done: list[str] = []
    futures: list = []
    lock = threading.Lock()

    async def _boom():
        raise RuntimeError("хранилище недоступно")

    async def _ok():
        done.append("ok")

    with lock:
        futures.append(_future_for(_boom(), loop))
        futures.append(_future_for(_ok(), loop))

    await _drain_trace_writes(futures, lock)

    assert done == ["ok"]
