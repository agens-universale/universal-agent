"""Вызов, отменённый в очереди за слотом, не исполняется вовсе.

Слотов у MCP восемь: при веере дискавери или нескольких агентах на одном loop
очередь реальна. Пока проверки после захвата слота не было, брошенный вызов
доходил до тела — то есть уходил на MCP-сервер с побочными эффектами уже
после того, как человек прервал ход.
"""

import asyncio
import threading

import pytest


async def test_a_call_cancelled_while_queued_never_runs():
    # Внутри теста: модули `tests/tools/` импортируются под заглушкой
    # `gc_core.tools` (см. conftest), и взятый при сборке `run_abandonable_in`
    # был бы из выброшенного экземпляра модуля.
    from gc_core.tools.connect_mcp.abandonable import run_abandonable_in

    slots = threading.BoundedSemaphore(1)
    holding, release = threading.Event(), threading.Event()
    ran: list = []

    def _first():
        holding.set()
        release.wait(5)
        return "первый"

    def _queued():
        ran.append("второй")
        return "второй"

    first = asyncio.ensure_future(
        run_abandonable_in(
            slots,
            _first,
            _thread_name="queue-holder",
        )
    )
    assert await asyncio.to_thread(holding.wait, 5), (
        "первый вызов не занял слот"
    )

    second = asyncio.ensure_future(
        run_abandonable_in(
            slots,
            _queued,
            _thread_name="queue-waiter",
        )
    )
    await asyncio.sleep(0.2)  # второй стоит в очереди за слотом
    second.cancel()  # Ctrl+C: ход сняли
    with pytest.raises(asyncio.CancelledError):
        await second

    release.set()
    assert await asyncio.wait_for(first, 5) == "первый"
    await asyncio.sleep(0.3)  # очереди дать пройти

    assert ran == [], "отменённый в очереди вызов всё равно исполнился"


async def test_cleanup_runs_even_if_the_call_never_started():
    """Уборка вызывающего зовётся и при раннем выходе.

    `on_abandoned` закрывает тело гварда, созданное ДО ухода в поток: не
    позвать его — значит оставить незакрытый генератор.
    """
    from gc_core.tools.connect_mcp.abandonable import run_abandonable_in

    slots = threading.BoundedSemaphore(1)
    holding, release = threading.Event(), threading.Event()
    cleaned = threading.Event()

    def _first():
        holding.set()
        release.wait(5)
        return "первый"

    first = asyncio.ensure_future(
        run_abandonable_in(
            slots,
            _first,
            _thread_name="queue-holder",
        )
    )
    assert await asyncio.to_thread(holding.wait, 5)

    second = asyncio.ensure_future(
        run_abandonable_in(
            slots,
            lambda: "не должен исполниться",
            _thread_name="queue-waiter",
            _on_abandoned=cleaned.set,
        )
    )
    await asyncio.sleep(0.2)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second

    release.set()
    await asyncio.wait_for(first, 5)

    assert await asyncio.to_thread(cleaned.wait, 5), (
        "уборка за брошенным вызовом не позвана — тело гварда осталось незакрытым"
    )
