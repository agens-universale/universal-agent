"""Гейт: параллельные прогоны не закрывают MCP-сессии друг другу.

История. Все три реестра живых MCP-сессий ключевались только по имени сервера
(``stream_*`` — ещё и по loop). ``AgentExecutor.arun`` закрывает сессии в своём
``finally``, поэтому первый доигравший агент сносил ЖИВЫЕ сессии остальных.

Симптомы различались по транспорту, и это сбивало с толку:

* ``stdio`` + persistent — тихо. Процесс убит, ``_ensure_started`` поднимал
  новый. Видно только по времени: в прогоне трёх агентов один занял 44.8 с
  вместо 18.8 с у соседа на той же задаче — дважды ждал старта Chromium.
* ``stream_*`` — падение. Сессии MCP SDK построены на anyio-scope'ах, вошли в
  них задачи-владельцы, закрытие из чужой задачи отменяет не ту:
  ``CancelledError: Cancelled via cancel scope … by <Task pending Task-5>``.
* свои ``http``/``sse`` — тоже тихо, teardown'а там нет вовсе: агенты просто
  делили один session-id, то есть одно состояние НА СЕРВЕРЕ.

Тесты ниже проверяют свойство на всех трёх реестрах сразу, без сети и
подпроцессов: важно не «как закрывается», а «чьё закрывается».
"""
import asyncio

import pytest

from gc_core.mcp_session_owner import (
    current_owner,
    new_owner_token,
    reset_owner,
    set_owner,
    use_owner,
)


def test_tokens_are_unique():
    assert new_owner_token() != new_owner_token()


def test_owner_is_none_outside_any_run():
    """Прямой apply_tool из теста или gc-mcp идёт вне прогона — это законно."""
    assert current_owner() is None


def test_use_owner_restores_previous():
    with use_owner("a"):
        assert current_owner() == "a"
        with use_owner("b"):
            assert current_owner() == "b"
        assert current_owner() == "a"
    assert current_owner() is None


def test_set_reset_pair():
    token = set_owner("x")
    assert current_owner() == "x"
    reset_owner(token)
    assert current_owner() is None


# --- persistent stdio ------------------------------------------------------

class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def stdio_registry(monkeypatch):
    from gc_core.tools.connect_mcp import persistent_stdio

    reg = persistent_stdio.PersistentStdioRegistry()
    monkeypatch.setattr(
        persistent_stdio, "PersistentStdioSession", lambda cfg: _FakeSession()
    )
    return reg


def test_stdio_sessions_are_per_owner(stdio_registry):
    with use_owner("agent-1"):
        a = stdio_registry.get_or_create("browser", {})
    with use_owner("agent-2"):
        b = stdio_registry.get_or_create("browser", {})
    assert a is not b, "одно имя сервера у разных владельцев дало общую сессию"


def test_stdio_same_owner_reuses(stdio_registry):
    with use_owner("agent-1"):
        first = stdio_registry.get_or_create("browser", {})
        second = stdio_registry.get_or_create("browser", {})
    assert first is second, "владелец обязан переиспользовать свою сессию"


def test_stdio_close_all_spares_other_owners(stdio_registry):
    """Ядро бага: teardown одного прогона не смеет трогать чужое."""
    with use_owner("agent-1"):
        mine = stdio_registry.get_or_create("browser", {})
    with use_owner("agent-2"):
        theirs = stdio_registry.get_or_create("browser", {})

    with use_owner("agent-1"):
        stdio_registry.close_all()

    assert mine.closed is True
    assert theirs.closed is False, "закрыли сессию работающего соседа"


def test_stdio_close_everything_closes_foreign_too(stdio_registry):
    """На выходе из процесса щадить некого — иначе Chromium переживёт агента."""
    with use_owner("agent-1"):
        mine = stdio_registry.get_or_create("browser", {})
    with use_owner("agent-2"):
        theirs = stdio_registry.get_or_create("browser", {})

    stdio_registry.close_everything()

    assert mine.closed is True and theirs.closed is True


# --- stream_* --------------------------------------------------------------

class _FakeCm:
    def __init__(self, log: list, name: str) -> None:
        self._log = log
        self._name = name

    async def __aexit__(self, *exc: object) -> None:
        self._log.append(self._name)


@pytest.mark.asyncio
async def test_stream_sessions_are_per_owner():
    from gc_core.tools.connect_mcp.stream_client import _PersistentSessionManager

    mgr = _PersistentSessionManager()
    with use_owner("agent-1"):
        mgr.set_session("stream_http::u", "session-1", ())
    with use_owner("agent-2"):
        mgr.set_session("stream_http::u", "session-2", ())

    with use_owner("agent-1"):
        assert mgr.get_session("stream_http::u") == "session-1"
    with use_owner("agent-2"):
        assert mgr.get_session("stream_http::u") == "session-2"


@pytest.mark.asyncio
async def test_stream_aclose_all_spares_other_owners():
    """Тот самый случай, что валил три агента с CancelledError."""
    from gc_core.tools.connect_mcp.stream_client import _PersistentSessionManager

    mgr = _PersistentSessionManager()
    closed: list = []
    with use_owner("agent-1"):
        mgr.set_session("s", "one", (_FakeCm(closed, "agent-1"),))
    with use_owner("agent-2"):
        mgr.set_session("s", "two", (_FakeCm(closed, "agent-2"),))

    with use_owner("agent-1"):
        await mgr.aclose_all()

    assert closed == ["agent-1"], f"закрыли лишнее: {closed}"
    with use_owner("agent-2"):
        assert mgr.get_session("s") == "two", "сессия соседа исчезла из кэша"


@pytest.mark.asyncio
async def test_stream_teardown_spares_a_foreign_owner():
    """``aclose_all`` закрывает сессии СВОЕГО владельца — и только их.

    Прежде здесь стоял синхронный ``close_all``, который забывал всё подряд,
    включая чужое. Это была не вторая форма того же, а ДРУГАЯ операция под
    похожим именем: она ничего не закрывала, только роняла ссылки. Прода у
    неё не было, и она снята.

    Узость корутинной формы — не недоделка, а условие корректности: чужой
    владелец в этот момент может быть внутри вызова тула, и закрытие из нашей
    задачи отменило бы его anyio-scope.
    """
    from gc_core.tools.connect_mcp.stream_client import _PersistentSessionManager

    mgr = _PersistentSessionManager()
    with use_owner("agent-1"):
        mgr.set_session("s", "one", ())
    with use_owner("agent-2"):
        mgr.set_session("s", "two", ())

    with use_owner("agent-1"):
        await mgr.aclose_all()
        assert mgr.get_session("s") is None
    with use_owner("agent-2"):
        assert mgr.get_session("s") == "two", "снесли сессию чужого владельца"


# --- свои http/sse: session-id --------------------------------------------

def test_own_http_session_ids_are_per_owner():
    """Один session-id на всех означал бы общее состояние НА СЕРВЕРЕ.

    Для браузерного MCP это буквально одна вкладка на двоих: агенты сбивали бы
    друг другу страницу, не получая при этом никакой ошибки.
    """
    from gc_core.tools.connect_mcp.client import _MCP_SESSIONS

    _MCP_SESSIONS.clear()
    with use_owner("agent-1"):
        _MCP_SESSIONS[(current_owner(), "srv")] = "sid-1"
    with use_owner("agent-2"):
        _MCP_SESSIONS[(current_owner(), "srv")] = "sid-2"

    assert _MCP_SESSIONS[("agent-1", "srv")] == "sid-1"
    assert _MCP_SESSIONS[("agent-2", "srv")] == "sid-2"
    _MCP_SESSIONS.clear()


# --- перенос владельца через границы исполнения ---------------------------

@pytest.mark.asyncio
async def test_owner_survives_to_thread():
    """Синхронные тулы уезжают в asyncio.to_thread — владелец обязан доехать."""
    with use_owner("agent-1"):
        seen = await asyncio.to_thread(current_owner)
    assert seen == "agent-1"


@pytest.mark.asyncio
async def test_owner_survives_abandonable_thread():
    """run_abandonable поднимает ГОЛЫЙ поток: контекст копируется вручную."""
    from gc_core.tools.connect_mcp.abandonable import run_abandonable

    with use_owner("agent-1"):
        seen = await run_abandonable(current_owner)
    assert seen == "agent-1"


@pytest.mark.asyncio
async def test_parallel_tasks_keep_separate_owners():
    """Каждая задача gather живёт со своим владельцем, они не путаются."""
    async def worker(name: str) -> str:
        with use_owner(name):
            await asyncio.sleep(0)
            return current_owner()

    got = await asyncio.gather(worker("a"), worker("b"), worker("c"))
    assert got == ["a", "b", "c"]
