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

import inspect

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


def test_the_manager_has_no_sync_teardown_at_all():
    """У менеджера сессий синхронных форм сноса нет — ни одной.

    Раньше сторожили, что ``_SESSION_MANAGER.invalidate(`` не встречается в
    источнике: формы существовали, и вернуть их в код было делом одной
    строки. Синхронный снос НЕ закрывает стек, он только забывает ссылку, —
    стек уезжает к сборщику мусора, и его закрытие швыряет ``CancelledError``
    в ход агента. Теперь форм просто нет, и вернуть их нечем.
    """
    mgr = sc._PersistentSessionManager()
    for name in ("close_all", "invalidate"):
        assert not hasattr(mgr, name), f"вернулась синхронная {name}"
    for name in ("aclose_all", "ainvalidate"):
        assert inspect.iscoroutinefunction(getattr(mgr, name))


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


# --- M15: teardown устойчив к отмене ---------------------------------------


async def test_teardown_finishes_even_when_the_caller_is_cancelled():
    """Закрытие сессий обязано ДОЙТИ до конца: это `finally` цикла агента.

    Раньше `aclose_all` вынимал сессии пачкой и закрывал последовательно:
    `CancelledError` посреди списка оставлял остаток ВНЕ реестра — то есть
    незакрытым и уже никому не видимым. Для playwright это значит, что
    Chromium переживает агента.
    """
    closed: list = []

    class _SlowSession:
        def __init__(self, name: str) -> None:
            self._name = name

        async def aclose(self) -> None:
            await asyncio.sleep(0.05)
            closed.append(self._name)

    registry = ps.PersistentStdioRegistry()
    from gc_core.mcp_session_owner import current_owner

    owner = current_owner()
    for name in ("a", "b", "c"):
        registry._sessions[(owner, name)] = _SlowSession(name)

    task = asyncio.create_task(registry.aclose_all())
    await asyncio.sleep(0.02)                  # дать начать первую
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # `shield` не задерживает отмену — он лишь не даёт ей прервать закрытие,
    # и то доигрывает в фоне. Ждём именно этого: смысл не в «успело до
    # возврата», а в «дошло до конца».
    for _ in range(50):
        if len(closed) == 3:
            break
        await asyncio.sleep(0.02)

    # Первая закрылась под shield, остальные — дочисткой в пределах общего
    # бюджета. Оставить их в реестре «до следующего сноса» нельзя: снос
    # фильтрует по владельцу, а следующий прогон приходит с новым.
    assert sorted(closed) == ["a", "b", "c"], closed
    assert not [k for k in registry._sessions if k[0] == owner], (
        f"остаток не закрыт: {registry._sessions}"
    )


async def test_teardown_closes_everything_when_not_cancelled():
    """Обычный путь не изменился: все свои сессии закрыты и сняты с реестра."""
    closed: list = []

    class _Session:
        def __init__(self, name: str) -> None:
            self._name = name

        async def aclose(self) -> None:
            closed.append(self._name)

    registry = ps.PersistentStdioRegistry()
    from gc_core.mcp_session_owner import current_owner

    owner = current_owner()
    for name in ("a", "b"):
        registry._sessions[(owner, name)] = _Session(name)

    await registry.aclose_all()

    assert sorted(closed) == ["a", "b"], closed
    assert not [k for k in registry._sessions if k[0] == owner]


# --- M14: закрытие не в своей задаче не должно проходить молча -------------


async def test_closing_from_a_foreign_task_warns(caplog):
    """Инвариант «открыли и закрыли в одной задаче» обязан быть слышен.

    Внутри стека anyio task group: выход из cancel scope в чужой задаче даёт
    `RuntimeError`, который `_aclose_stack` глотает, — сессия остаётся
    незакрытой МОЛЧА. Сегодня инвариант держится сам собой (тулы ждутся
    инлайн), но сломает его первый же параллельный диспетчер.
    """
    import logging

    manager = sc._PersistentSessionManager()

    async def _open() -> None:
        manager.set_session("srv", object(), (_Stack([], "t"),))

    await asyncio.create_task(_open())         # открыли в ОДНОЙ задаче

    with caplog.at_level(logging.WARNING):
        await manager.ainvalidate("srv")       # закрываем в ДРУГОЙ

    assert any("не в той задаче" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


async def test_closing_in_the_same_task_is_silent():
    """Обратная половина: правильный порядок не должен шуметь."""
    import logging

    manager = sc._PersistentSessionManager()
    manager.set_session("srv", object(), (_Stack([], "t"),))

    logger = logging.getLogger("gc_core.tools.connect_mcp.stream_client")
    records: list = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        await manager.ainvalidate("srv")
    finally:
        logger.removeHandler(handler)

    assert records == [], [r.getMessage() for r in records]
