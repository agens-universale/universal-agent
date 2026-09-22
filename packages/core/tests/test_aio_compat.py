"""run_coro_blocking: дождаться корутины из синхронного кода (Задача 5 плана).

Раньше ту же дыру ядро закрывало через глобальный monkey-patch asyncio.run
в gc_server — он действовал на весь процесс, включая код встраивающего.
"""
import asyncio
import threading

import pytest
from gc_core.utils.aio_compat import run_coro_blocking


def test_runs_without_running_loop():
    async def work():
        return 7

    assert run_coro_blocking(work()) == 7


def test_runs_inside_running_loop():
    """Главный случай: синхронный код позвали из-под живого loop.

    Так ядро зовут TUI (loop Textual) и сервер (loop uvicorn); голый
    asyncio.run здесь падает «cannot be called from a running event loop».
    """
    async def outer():
        async def work():
            return 8

        return run_coro_blocking(work())

    assert asyncio.run(outer()) == 8


def test_propagates_exception_inside_running_loop():
    async def outer():
        async def boom():
            raise KeyError("к")

        return run_coro_blocking(boom())

    with pytest.raises(KeyError, match="к"):
        asyncio.run(outer())


def test_leaves_no_threads_behind():
    """Одноразовый поток закрывается: это костыль для редких мест, а не пул."""
    before = threading.active_count()

    async def outer():
        async def work():
            return 1

        return run_coro_blocking(work())

    asyncio.run(outer())
    assert threading.active_count() == before
