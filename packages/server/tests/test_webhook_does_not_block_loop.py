"""Эндпоинт вебхука не морозит event loop сервера.

Сборка ``RunContext`` включает MCP-дискавери: сетевые вызовы к каждому серверу
с общим дедлайном. Синхронный ``create_run_context`` держал на ней loop uvicorn
целиком — а с переездом ядра на asyncio прогоны стали тасками на ТОМ ЖЕ loop,
поэтому заморозка била разом по всем эндпоинтам и по всем идущим прогонам.

Строка была идентична до-миграционной, то есть блокировка досталась в
наследство; переезд лишь расширил радиус поражения. Тест сторожит, чтобы она не
вернулась молча.
"""
import asyncio
import time

import pytest

# Сборка «идёт» столько; порог разрыва loop заведомо меньше.
_BUILD_SEC = 0.5
_MAX_STALL_SEC = 0.15


@pytest.fixture()
def blocking_ctx(monkeypatch):
    """Подменить дискавери — самую долгую часть сборки — медленной.

    Подменяем именно её, а не ``create_run_context`` целиком: с тех пор как
    ``acreate_run_context`` стала настоящей корутиной, синхронную сборку она не
    зовёт вовсе. Дискавери и есть 99.7% времени (866–1405 мс против 2.4–6.9 мс
    на всё остальное), так что мерить надо её.

    Заглушка СИНХРОННАЯ и блокирующая — специально: она изображает легаси-
    транспорт, который остаётся в потоке. Если такой транспорт когда-нибудь
    начнут звать прямо на loop, тест это увидит.
    """
    import gc_core.tools as tools_pkg

    def _slow(config_dict, on_progress=None, **kwargs):
        time.sleep(_BUILD_SEC)
        return {}, {}


    async def _aslow(config_dict, on_progress=None, **kwargs):
        import asyncio as _a

        return await _a.to_thread(_slow, config_dict, on_progress, **kwargs)

    import gc_core.tools.connect_mcp.agent_tools as agent_tools

    monkeypatch.setattr(agent_tools, "aget_mcp_tools_data_for_config", _aslow)

    import gc_core.run_context as rc

    return rc


async def _heartbeat(stop: asyncio.Event, gaps: list) -> None:
    prev = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(0.02)
        now = time.monotonic()
        gaps.append(now - prev)
        prev = now


async def test_context_build_leaves_the_loop_free(blocking_ctx):
    """Пока контекст собирается, соседняя корутина продолжает тикать."""
    gaps: list = []
    stop = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(stop, gaps))
    await asyncio.sleep(0.05)

    started = time.monotonic()
    await blocking_ctx.acreate_run_context({}, auth_connector=None)
    elapsed = time.monotonic() - started

    stop.set()
    await beat

    assert elapsed >= _BUILD_SEC, "подмена не сработала — тест ничего не меряет"
    assert gaps, "хартбит не тикал вовсе"
    assert max(gaps) < _MAX_STALL_SEC, (
        f"loop залипал на {max(gaps):.3f}с при сборке контекста — "
        f"похоже, дискавери зовётся прямо на loop"
    )


def test_webhook_awaits_the_async_builder():
    """Эндпоинт зовёт именно асинхронный сборщик.

    Страховка от «поправили и откатили»: тест выше поймает регрессию только
    если она сохранит подменяемое имя, а этот — читает сам call-site.
    """
    import inspect

    from gc_server import server

    src = inspect.getsource(server.events_webhook)
    assert "await acreate_run_context(" in src, (
        "вебхук снова собирает контекст синхронно"
    )
