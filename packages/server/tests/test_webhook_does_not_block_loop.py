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
    """Подменить сборку контекста синхронной и медленной, как MCP-дискавери."""
    import gc_core.run_context as rc

    def _slow(config_dict, **kwargs):
        time.sleep(_BUILD_SEC)
        return object()

    monkeypatch.setattr(rc, "create_run_context", _slow)
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
        f"похоже, вернулся синхронный create_run_context"
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
