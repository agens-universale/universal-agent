"""Синхронные обёртки stream_client безопасны и с живым event loop, и без.

Регрессия, от которой защищает: замена ``run_coro_blocking`` обратно на
``asyncio.run()`` молча роняет листинг/вызовы MCP-тулов из loop-треда — агент
остаётся без MCP-тулов без единой ошибки на экране.

Раньше тест проверял алиас ``stream_client._run_coro``. С переездом MCP на
нативный async (Задача 20) алиас исчез: у каждого транспорта появилась
асинхронная точка ``a*``, а синхронная стала шимом поверх неё. Свойство то же,
проверяется теперь на самих шимах.
"""

import asyncio
import inspect

import pytest

from gc_core.tools.connect_mcp import stream_client
from gc_core.utils.aio_compat import run_coro_blocking


async def _coro_value():
    return 42


async def _coro_raises():
    raise ValueError("boom")


def test_run_coro_without_running_loop():
    assert run_coro_blocking(_coro_value()) == 42


def test_run_coro_inside_running_loop():
    async def main():
        return run_coro_blocking(_coro_value())

    assert asyncio.run(main()) == 42


def test_run_coro_propagates_exception_without_loop():
    with pytest.raises(ValueError, match="boom"):
        run_coro_blocking(_coro_raises())


def test_run_coro_propagates_exception_inside_running_loop():
    async def main():
        return run_coro_blocking(_coro_raises())

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(main())


#: Шесть точек входа: листинг и вызов тула на каждый из трёх SDK-транспортов.
_PAIRS = [
    (f"{prefix}_stream_{transport}", f"a{prefix}_stream_{transport}")
    for prefix in ("list_tools", "call_tool")
    for transport in ("sse", "http", "stdio")
]


@pytest.mark.parametrize("sync_name,async_name", _PAIRS)
def test_every_transport_has_an_async_twin(sync_name, async_name):
    """У каждой синхронной точки есть асинхронная, и типы не перепутаны."""
    sync_fn = getattr(stream_client, sync_name)
    async_fn = getattr(stream_client, async_name)

    assert not inspect.iscoroutinefunction(sync_fn), sync_name
    assert inspect.iscoroutinefunction(async_fn), async_name


@pytest.mark.parametrize("sync_name,async_name", _PAIRS)
def test_sync_twin_is_a_shim_over_the_async_one(sync_name, async_name):
    """Реализация одна: шим обязан звать асинхронного двойника, а не свою копию.

    Две копии логики выбора persistent/stateless разъехались бы молча —
    именно так уже случалось с правилом выбора транспорта.
    """
    src = inspect.getsource(getattr(stream_client, sync_name))
    assert "run_coro_blocking" in src, sync_name
    assert async_name in src, f"{sync_name} не делегирует в {async_name}"


def test_no_background_thread_left_in_stream_client():
    """Гейт против отката: фоновый loop-поток и пул исполнителей убраны.

    До Задачи 20 здесь жил вечный event loop в демон-потоке, потому что ядро
    было синхронным и корутину SDK негде было исполнить. Теперь сессии живут
    на loop вызывающего.
    """
    import ast
    from pathlib import Path

    # Смотрим на КОД, а не на текст файла: слова «threading.Thread» и
    # «run_coroutine_threadsafe» законно встречаются в докстроках, где описано,
    # как было раньше. Грубый поиск по строке ловил бы собственную историю.
    tree = ast.parse(Path(stream_client.__file__).read_text(encoding="utf-8"))
    used = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in (
        "threading.Thread",
        "concurrent.futures.ThreadPoolExecutor",
        "ThreadPoolExecutor",
        "asyncio.run_coroutine_threadsafe",
    ):
        assert forbidden not in used, f"вернулся {forbidden}"
