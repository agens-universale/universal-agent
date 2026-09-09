"""Снос и закрытие MCP-сессий не должны бить по вызывающему.

Два дефекта одного класса — «ресурс, открытый на одном loop и в одной задаче,
закрывается неизвестно где»:

* **B1.** ``invalidate()`` выбрасывал стек ``transport + ClientSession`` из
  словарей, не закрывая: «loop может быть чужим». Для ошибочной ветки это
  неверно — мы ровно в той задаче, где сессию открывали. Брошенный стек
  доставался сборщику мусора, тот ставил ``aclose()`` асинхронного генератора
  ОТДЕЛЬНОЙ задачей, выход из anyio cancel scope в чужой задаче давал
  ``RuntimeError`` и швырял посторонний ``CancelledError`` в ход агента.
  ``aapply_tool`` ловит только ``Exception``, поэтому отмена прошивала ход
  насквозь. Триггер — любая ошибка вызова при ``persistent_session: true``.

* **H1.** Асинхронное закрытие брало ``threading.Lock`` блокирующе. Лок держит
  ``call()`` на весь обмен, а обмен идёт в потоке, который переживает отмену
  вызывающего: сняли ход по дедлайну — поток остался в ``readline`` под локом,
  и ``aclose_all`` из ``finally`` встал на локе прямо на loop.
"""
import asyncio
import threading
import time

import pytest

from gc_core.tools.connect_mcp import persistent_stdio as ps
from gc_core.tools.connect_mcp import stream_client as sc


# --- B1: снос сессии закрывает стек здесь же -------------------------------


class _Stack:
    """Двойник контекст-менеджера из стека сессии."""

    def __init__(self, log: list, name: str) -> None:
        self._log = log
        self._name = name

    async def __aexit__(self, *_exc) -> None:
        self._log.append((self._name, asyncio.current_task().get_name()))


async def test_ainvalidate_closes_the_stack_in_the_calling_task():
    """Стек закрывается СРАЗУ и в ТОЙ ЖЕ задаче, где сессию открывали."""
    closed: list = []
    manager = sc._PersistentSessionManager()
    manager.set_session("srv", object(), (_Stack(closed, "transport"), _Stack(closed, "session")))

    here = asyncio.current_task().get_name()
    await manager.ainvalidate("srv")

    assert [name for name, _ in closed] == ["session", "transport"], (
        f"стек закрыт не в обратном порядке или не закрыт вовсе: {closed}"
    )
    assert all(task == here for _, task in closed), (
        f"закрытие ушло в чужую задачу: {closed} (мы в {here})"
    )


async def test_ainvalidate_forgets_the_session_too():
    """Забыть — по-прежнему обязанность: сессия негодна и переиспользованию не подлежит."""
    manager = sc._PersistentSessionManager()
    manager.set_session("srv", object(), (_Stack([], "t"),))

    await manager.ainvalidate("srv")

    assert manager.get_session("srv") is None


async def test_ainvalidate_survives_a_broken_stack():
    """Ошибка закрытия не должна подменять причину, по которой сессию сносят."""
    class _Boom:
        async def __aexit__(self, *_exc):
            raise RuntimeError("закрытие не удалось")

    manager = sc._PersistentSessionManager()
    manager.set_session("srv", object(), (_Boom(),))

    await manager.ainvalidate("srv")          # не должно бросить

    assert manager.get_session("srv") is None


def test_no_bare_invalidate_left_on_the_async_path():
    """На асинхронном пути голого ``invalidate`` быть не должно."""
    import pathlib

    src = pathlib.Path(sc.__file__).read_text(encoding="utf-8")

    assert "_SESSION_MANAGER.invalidate(" not in src, (
        "вернулся синхронный снос сессии: стек уедет к сборщику мусора, "
        "и его закрытие швырнёт CancelledError в ход агента"
    )


# --- H1: закрытие не встаёт на локе ----------------------------------------


async def test_aclose_does_not_wait_for_a_held_lock():
    """Занятый лок не должен морозить loop на всё время чужого обмена.

    Держим лок из другого потока — ровно так выглядит abandonable-поток,
    переживший отмену вызывающего, — и закрываем сессию с loop.
    """
    session = ps.PersistentStdioSession({"command": "true"})
    session._proc = None                       # процесса нет: меряем только лок

    released = threading.Event()

    def _hold() -> None:
        with session._lock:
            released.wait(5)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    time.sleep(0.05)                           # дать потоку взять лок

    try:
        started = time.perf_counter()
        await asyncio.wait_for(session.aclose(), 2)
        spent = time.perf_counter() - started
    finally:
        released.set()
        holder.join(5)

    assert spent < 1.0, (
        f"закрытие ждало лок {spent:.2f}с — на живом loop это заморозка "
        f"до request_timeout_sec"
    )


async def test_aclose_still_takes_the_process_when_the_lock_is_free():
    """Обычный путь не изменился: свободный лок — забираем процесс под ним."""
    session = ps.PersistentStdioSession({"command": "true"})
    session._proc = None

    await session.aclose()

    assert session._proc is None
