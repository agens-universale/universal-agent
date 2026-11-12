"""Сервис MCP у фасада ACP (спека §5.1, §5.12, §6).

Сервис один на процесс: его создаёт ``build_facade``, агент каждой сессии
ACP открывает у него своего потребителя (``mcp_service=`` фабрики), сессии
MCP живут, пока живёт агент сессии. ``shutdown`` закрывает агентов, затем
сервис — тот закрывает и потребителей агентов с идущим ходом.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os

import pytest
from gc_acp import main as acp_main
from gc_acp.facade import AcpFacade
from gc_core.tools.connect_mcp.registry import ToolsCache
from gc_testkit.mcp import alive, exits, launches, stdio_cfg, wait_for
from gc_testkit.mcp.fixtures import mcp_home, mcp_reaper
from gc_testkit.settings import make_settings
from test_facade import (
    FakeAgent,
    MemoryWriter,
    _by_id,
    _init,
    _line,
    _new,
)

#: Потолок одного ожидания: зависание не повесит прогон.
_CEILING_SEC = 30.0


class FakeService:
    """Записывающий двойник сервиса MCP: только закрытие."""

    def __init__(self, events: list[str], error: Exception | None = None):
        self.events = events
        self.error = error
        self.closed = False

    async def aclose(self, budget: float = 10.0) -> None:
        self.events.append("service.aclose")
        self.closed = True
        if self.error is not None:
            raise self.error


def _open(tmp_path, *, events=None, service=None):
    """Фасад с подставной фабрикой; агенты пишут своё закрытие в
    ``events``."""
    agents: list[FakeAgent] = []
    events = [] if events is None else events

    async def factory(config, **kwargs):
        agent = FakeAgent()
        agent.factory_kwargs = kwargs
        index = len(agents)

        async def aclose():
            events.append(f"agent{index}.aclose")
            agent.closed = True

        agent.aclose = aclose
        agents.append(agent)
        return agent

    extra = {} if service is None else {"mcp_service": service}
    facade = AcpFacade(
        settings=make_settings(
            {"paths.threads_data_path": str(tmp_path), "os_backend": "local"}
        ),
        writer=MemoryWriter(),
        agent_factory=factory,
        **extra,
    )
    return facade, facade._writer, agents


async def _hang_turn(facade, writer, agent) -> None:
    """Ход сессии висит и не отпускается остановкой."""
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agent.hold = True
    agent.release_on_stop = False
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "a"}],
            },
            3,
        )
    )
    await asyncio.wait_for(agent.started.wait(), _CEILING_SEC)


async def test_factory_receives_service_not_cache(tmp_path):
    fake = FakeService([])
    facade, _writer, agents = _open(tmp_path, service=fake)
    await _init(facade)
    await _new(facade, "/work")

    (agent,) = agents
    assert agent.factory_kwargs["mcp_service"] is fake
    assert "tools_cache" not in agent.factory_kwargs
    await facade.shutdown()


async def test_shutdown_closes_service_after_agents(tmp_path):
    events: list[str] = []
    fake = FakeService(events)
    facade, _writer, _agents = _open(tmp_path, events=events, service=fake)
    await _init(facade)
    await _new(facade, "/a", request_id=2)
    await _new(facade, "/b", request_id=3)

    await asyncio.wait_for(facade.shutdown(), _CEILING_SEC)

    assert events == ["agent0.aclose", "agent1.aclose", "service.aclose"]


async def test_shutdown_with_running_turn_still_closes_service(tmp_path):
    fake = FakeService([])
    facade, writer, agents = _open(tmp_path, service=fake)
    await _init(facade)
    await _new(facade, "/work")
    await _hang_turn(facade, writer, agents[0])

    await asyncio.wait_for(facade.shutdown(), _CEILING_SEC)

    # Ход не ждём и агента не закрываем; его потребителя закроет сервис.
    assert agents[0].closed is False
    assert fake.closed
    agents[0].release.set()


async def test_session_close_keeps_service_open(tmp_path):
    fake = FakeService([])
    facade, writer, agents = _open(tmp_path, service=fake)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]

    await facade.handle_line(
        _line("session/close", {"sessionId": session_id}, 3)
    )

    assert _by_id(writer, 3)["result"] == {}
    assert agents[0].closed is True
    assert not fake.closed
    await facade.shutdown()
    assert fake.closed


async def test_shutdown_without_service(tmp_path):
    facade, _writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")

    await asyncio.wait_for(facade.shutdown(), _CEILING_SEC)

    assert agents[0].closed is True
    assert agents[0].factory_kwargs["mcp_service"] is None


async def test_service_close_failure_does_not_break_shutdown(tmp_path, caplog):
    events: list[str] = []
    fake = FakeService(events, error=RuntimeError("сервис не закрылся"))
    facade, _writer, _agents = _open(tmp_path, events=events, service=fake)
    await _init(facade)
    await _new(facade, "/work")

    with caplog.at_level(logging.ERROR, logger="gc_acp.facade"):
        await asyncio.wait_for(facade.shutdown(), _CEILING_SEC)

    assert events == ["agent0.aclose", "service.aclose"]
    assert "сервис не закрылся" in caplog.text


def test_build_facade_makes_service_with_backend(monkeypatch, tmp_path):
    created: list[dict] = []
    caches: list[object] = []
    backend = object()

    class RecordingService:
        def __init__(self, **kwargs):
            created.append(kwargs)

    def no_cache(self, *args, **kwargs):
        caches.append(self)

    monkeypatch.setattr(acp_main, "McpService", RecordingService)
    monkeypatch.setattr(acp_main, "resolve_os_backend", lambda _l: backend)
    monkeypatch.setattr(ToolsCache, "__init__", no_cache)
    loaded = make_settings(
        {"paths.threads_data_path": str(tmp_path), "os_backend": "local"}
    )

    facade = acp_main.build_facade(loaded, io.StringIO())

    assert created == [{"os_backend": backend, "connect_on_start": True}]
    assert isinstance(facade._mcp_service, RecordingService)
    assert facade._os_backend is backend
    assert caches == []


@pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")
async def test_acp_sessions_own_separate_mcp_processes(
    mcp_home, tmp_path, monkeypatch, mcp_reaper
):
    """Настоящий сервис, настоящие агенты сессий и stdio-сервер."""
    control = tmp_path / "srv.json"
    loaded = make_settings(
        {
            "paths.threads_data_path": str(tmp_path / "threads"),
            "os_backend": "local",
            "mcp.servers": {"srv": stdio_cfg(control)},
            "mcp.meta.discovery_timeout_sec": 30,
            "logging.save_logs": False,
        }
    )
    monkeypatch.chdir(tmp_path)
    facade = acp_main.build_facade(loaded, io.StringIO())
    writer = MemoryWriter()
    facade._writer = writer
    mcp_reaper.track(facade._mcp_service)
    await _init(facade)
    for request_id in (2, 3):
        await asyncio.wait_for(
            _new(facade, str(tmp_path), request_id), _CEILING_SEC
        )
    first, second = (
        _by_id(writer, request_id)["result"]["sessionId"]
        for request_id in (2, 3)
    )
    await wait_for(lambda: len(launches(control)) == 2, _CEILING_SEC)
    first_pid, second_pid = launches(control)

    await asyncio.wait_for(
        facade.handle_line(_line("session/close", {"sessionId": first}, 4)),
        _CEILING_SEC,
    )

    assert _by_id(writer, 4)["result"] == {}
    assert exits(control) == [first_pid]
    assert alive(second_pid)
    assert second in facade._sessions

    await asyncio.wait_for(facade.shutdown(), _CEILING_SEC)

    assert exits(control) == [first_pid, second_pid]
    assert facade._mcp_service.closed
