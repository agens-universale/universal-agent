"""Контракт _run_coro: синхронные обёртки stream_client безопасны при
работающем event loop (TUI/Textual) и без него (CLI).

Регрессия, от которой защищает: замена _run_coro обратно на asyncio.run()
молча роняет листинг/вызовы MCP-тулов из loop-треда — агент остаётся
без MCP-тулов без единой ошибки на экране.
"""

import asyncio

import pytest

from tools.connect_mcp.stream_client import _run_coro


async def _coro_value():
    return 42


async def _coro_raises():
    raise ValueError("boom")


def test_run_coro_without_running_loop():
    assert _run_coro(_coro_value()) == 42


def test_run_coro_inside_running_loop():
    async def main():
        return _run_coro(_coro_value())

    assert asyncio.run(main()) == 42


def test_run_coro_propagates_exception_without_loop():
    with pytest.raises(ValueError, match="boom"):
        _run_coro(_coro_raises())


def test_run_coro_propagates_exception_inside_running_loop():
    async def main():
        return _run_coro(_coro_raises())

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(main())
