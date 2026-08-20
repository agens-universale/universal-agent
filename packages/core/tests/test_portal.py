"""Портал: синхронный вход в асинхронное ядро (план async-переезда, Задача 2)."""
import asyncio
import threading

import pytest

from gc_core.utils.portal import (
    Portal,
    SyncApiInsideEventLoopError,
    get_portal,
    shutdown_portal,
)


def test_run_returns_coroutine_result():
    p = Portal()
    try:
        async def work():
            await asyncio.sleep(0)
            return 42

        assert p.run(work()) == 42
    finally:
        p.close()


def test_run_propagates_exception():
    """Исключение из корутины приходит вызывающему как своё, а не как чужое."""
    p = Portal()
    try:
        async def boom():
            raise ValueError("бум")

        with pytest.raises(ValueError, match="бум"):
            p.run(boom())
    finally:
        p.close()


def test_loop_is_reused_across_calls():
    """Loop один на портал: иначе каждый синхронный вызов заводил бы свой поток."""
    p = Portal()
    try:
        async def loop_id():
            return id(asyncio.get_running_loop())

        assert p.run(loop_id()) == p.run(loop_id())
    finally:
        p.close()


def test_run_executes_off_the_calling_thread():
    p = Portal()
    try:
        async def where():
            return threading.get_ident()

        assert p.run(where()) != threading.get_ident()
    finally:
        p.close()


def test_run_from_running_loop_raises():
    """Синхронный API из живого loop — ошибка вызывающего, а не тихая блокировка.

    Ровно этот случай `gc_server` закрывал глобальным monkey-patch `asyncio.run`
    (снимается Задачей 5): библиотека не имеет права ни вставать намертво, ни
    менять asyncio под встраивающим приложением.
    """
    p = Portal()

    async def outer():
        async def work():
            return 1

        with pytest.raises(SyncApiInsideEventLoopError, match="arun"):
            p.run(work())

    try:
        asyncio.run(outer())
    finally:
        p.close()


def test_close_is_idempotent():
    p = Portal()

    async def work():
        return "ok"

    assert p.run(work()) == "ok"
    p.close()
    p.close()  # второй раз — no-op, а не падение


def test_portal_is_lazy_and_thread_is_daemon():
    """До первого синхронного вызова потока нет; появившийся — демонский.

    Ленивость важна для встраивающих: импорт gc_core не должен заводить
    потоков. Демонский — чтобы забытый портал не держал выход процесса.
    """
    shutdown_portal()
    assert not any(t.name.startswith("gc-core-portal") for t in threading.enumerate())

    async def work():
        return "ok"

    try:
        assert get_portal().run(work()) == "ok"
        t = next(t for t in threading.enumerate() if t.name.startswith("gc-core-portal"))
        assert t.daemon is True
    finally:
        shutdown_portal()

    assert not any(t.name.startswith("gc-core-portal") for t in threading.enumerate())


def test_get_portal_returns_the_same_instance():
    shutdown_portal()
    try:
        assert get_portal() is get_portal()
    finally:
        shutdown_portal()
