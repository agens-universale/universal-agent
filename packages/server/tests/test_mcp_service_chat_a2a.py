"""Сервис MCP на поверхностях chat и A2A (спека §5.1, §5.3, §5.12, §6).

Поверхность создаёт сервис с подключением «при первом вызове», агенты
фасадов открывают у него своих потребителей через фабрику процесса,
остановка поверхности снимает ходы, закрывает сервис, затем агентов. Процессы тестового
stdio-сервера настоящие; модель не нужна: тул вызывается обёрткой из
набора агента — той, что вызвал бы цикл.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time

import pytest
import yaml
from gc_core.aio.agent import AsyncAgent
from gc_core.run_context import arefresh_context_tools
from gc_core.tools.connect_mcp import service as service_module
from gc_core.tools.connect_mcp.registry import ToolsCache, mcp_tool_full_name
from gc_core.tools.connect_mcp.service import McpService
from gc_server import app as app_mod
from gc_server.a2a import executor as a2a_executor_mod
from gc_server.a2a import surface as a2a_surface_mod
from gc_server.a2a.surface import A2aSurface
from gc_server.chat import surface as chat_surface_mod
from gc_server.chat.completions import facade as completions_mod
from gc_server.chat.responses import facade as responses_mod
from gc_server.chat.surface import ChatSurface
from gc_testkit.mcp import (
    FakeClock,
    alive,
    exits,
    launches,
    set_behavior,
    stdio_cfg,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.paths import REPO
from server_harness import runtime

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
#: Бюджет закрытия сервиса по умолчанию (``McpService.aclose``).
_SERVICE_BUDGET_SEC = 10.0
_ECHO = mcp_tool_full_name("srv", "echo")
_POSIX = pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")


def _settings(control) -> dict:
    return {
        "os_backend": "local",
        "agent.model": "m",
        "mcp.servers": {"srv": stdio_cfg(control)},
        "mcp.meta.discovery_timeout_sec": 30,
        "logging.save_logs": False,
    }


def _chat(tmp_path, control, config: dict | None = None) -> ChatSurface:
    return ChatSurface(runtime(tmp_path, config=config, **_settings(control)))


def _a2a(tmp_path, control, config: dict | None = None) -> A2aSurface:
    return A2aSurface(runtime(tmp_path, config=config, **_settings(control)))


_SURFACES = {"chat": _chat, "a2a": _a2a}
#: Модуль с потолком живых агентов у кэша агентов поверхности.
_CACHES = {"chat": responses_mod, "a2a": a2a_executor_mod}


def _agent_for(surface, name: str):
    """Кэш агентов поверхности: Responses у chat, исполнитель у A2A."""
    if name == "chat":
        return surface.responses._agent_for
    return surface.executor._agent_for


class _Recorder:
    """Подставная фабрика: запоминает именованные аргументы."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.kwargs: list[dict] = []
        self._events = events

    async def __call__(self, settings, **kwargs):
        del settings
        self.kwargs.append(kwargs)
        return _Agent(f"agent{len(self.kwargs) - 1}", self._events)


class _Agent:
    def __init__(self, name: str, events: list[str] | None) -> None:
        self.name = name
        self._events = events
        self.closed = False
        self.executor = type("E", (), {})()
        self.executor._ctx = type("C", (), {})()

    async def aclose(self) -> None:
        self.closed = True
        if self._events is not None:
            self._events.append(f"{self.name}.aclose")


class _FakeService:
    def __init__(self, events: list[str], error: Exception | None = None):
        self._events = events
        self._error = error
        self.closed = False

    async def aclose(self) -> None:
        self._events.append("service.aclose")
        self.closed = True
        if self._error is not None:
            raise self._error


def _install_fake_service(monkeypatch, service) -> None:
    for module in (chat_surface_mod, a2a_surface_mod):
        monkeypatch.setattr(module, "McpService", lambda **_kw: service)


async def _echo_pid(agent) -> int:
    """Вызов ``echo`` обёрткой из набора агента — той, что вызовет цикл."""
    tool = agent.executor._ctx.tools_data[_ECHO]["tool_fabric"]()
    envelope = await asyncio.wait_for(tool.acall(text="эхо"), _CEILING_SEC)
    (content,) = json.loads(envelope)["result"]["content"]
    return json.loads(content["text"])["pid"]


async def test_chat_facades_pass_service_to_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(responses_mod, "bind_ask_human", lambda _a: None)
    surface = _chat(tmp_path, tmp_path / "srv.json")
    factory = _Recorder()
    surface.responses._agent_factory = factory
    surface.completions._agent_factory = factory

    await surface.responses._agent_for("t", str(tmp_path))
    await surface.completions._agent_for("t", str(tmp_path))

    service = surface._mcp_service
    assert isinstance(service, McpService)
    assert [kw["mcp_service"] for kw in factory.kwargs] == [service] * 2
    assert all("tools_cache" not in kw for kw in factory.kwargs)


async def test_a2a_executor_passes_service(tmp_path):
    surface = _a2a(tmp_path, tmp_path / "srv.json")
    factory = _Recorder()
    surface.executor._agent_factory = factory

    await surface.executor._agent_for("ctx", str(tmp_path))

    (kwargs,) = factory.kwargs
    assert isinstance(surface._mcp_service, McpService)
    assert kwargs["mcp_service"] is surface._mcp_service
    assert "tools_cache" not in kwargs


def test_surfaces_make_first_call_services_without_cache(
    tmp_path, monkeypatch
):
    """Сервис на поверхность, общий для Responses и Completions; у A2A —
    свой. Политика — «при первом вызове», бэкенд — поверхности."""
    created: list[dict] = []
    caches: list[object] = []
    backend = object()

    class RecordingService:
        def __init__(self, **kwargs):
            created.append(kwargs)

    def no_cache(self, *_args, **_kwargs):
        caches.append(self)

    for module in (chat_surface_mod, a2a_surface_mod):
        monkeypatch.setattr(module, "McpService", RecordingService)
        monkeypatch.setattr(module, "resolve_os_backend", lambda _l: backend)
    monkeypatch.setattr(ToolsCache, "__init__", no_cache)

    chat = _chat(tmp_path, tmp_path / "srv.json")
    a2a = _a2a(tmp_path, tmp_path / "srv.json")

    expected = {"os_backend": backend, "connect_on_start": False}
    assert created == [expected, expected]
    assert chat.responses._mcp_service is chat._mcp_service
    assert chat.completions._mcp_service is chat._mcp_service
    assert a2a.executor._mcp_service is a2a._mcp_service
    assert chat._mcp_service is not a2a._mcp_service
    assert caches == []


async def test_runtime_factory_forwards_service_to_agent(
    tmp_path, monkeypatch
):
    calls: list[dict] = []

    async def recording_create(settings, **kwargs):
        del settings
        calls.append(kwargs)
        return "агент"

    monkeypatch.setattr(app_mod.AsyncAgent, "create", recording_create)
    srv_runtime = runtime(tmp_path)
    factory = srv_runtime.agent_factory(srv_runtime.base, surface="chat")
    service = object()

    agent = await factory(
        srv_runtime.base,
        auth_connector=None,
        mcp_service=service,
        os_backend=None,
    )

    (kwargs,) = calls
    assert agent == "агент"
    assert kwargs["mcp_service"] is service
    assert "tools_cache" not in kwargs


@pytest.mark.parametrize("name", ["chat", "a2a"])
async def test_surface_stop_closes_service_and_is_repeatable(name, tmp_path):
    surface = _SURFACES[name](tmp_path, tmp_path / "srv.json")
    service = surface._mcp_service

    # Процесс зовёт ``astop`` дважды: из ``watch`` и из ``finally``.
    await asyncio.wait_for(surface.astop(), _CEILING_SEC)
    await asyncio.wait_for(surface.astop(), _CEILING_SEC)

    assert service.closed


@pytest.mark.parametrize("name", ["chat", "a2a"])
async def test_stop_turns_then_service_then_agents(
    name, tmp_path, monkeypatch
):
    monkeypatch.setattr(responses_mod, "bind_ask_human", lambda _a: None)
    events: list[str] = []
    _install_fake_service(monkeypatch, _FakeService(events))
    surface = _SURFACES[name](tmp_path, tmp_path / "srv.json")
    factory = _Recorder(events)
    agent_for = _agent_for(surface, name)
    facade = agent_for.__self__
    facade._agent_factory = factory
    await agent_for("t", str(tmp_path))
    stop_turns = facade.stop_turns

    async def recording_stop_turns() -> None:
        events.append("turns.stop")
        await stop_turns()

    monkeypatch.setattr(facade, "stop_turns", recording_stop_turns)

    await asyncio.wait_for(surface.astop(), _CEILING_SEC)

    # ``shutdown`` снимает ходы ещё раз (уже вхолостую), затем агентов.
    assert events == [
        "turns.stop",
        "service.aclose",
        "turns.stop",
        "agent0.aclose",
    ]


@pytest.mark.parametrize("name", ["chat", "a2a"])
async def test_service_close_failure_does_not_break_stop(
    name, tmp_path, monkeypatch, caplog
):
    events: list[str] = []
    error = RuntimeError("сервис не закрылся")
    _install_fake_service(monkeypatch, _FakeService(events, error))
    surface = _SURFACES[name](tmp_path, tmp_path / "srv.json")

    with caplog.at_level(logging.ERROR, logger="gc_server.surface"):
        await asyncio.wait_for(surface.astop(), _CEILING_SEC)

    assert events == ["service.aclose"]
    assert "сервис не закрылся" in caplog.text


@pytest.mark.parametrize("name", ["chat", "a2a"])
async def test_service_closes_when_facade_stop_fails(
    name, tmp_path, monkeypatch
):
    events: list[str] = []
    _install_fake_service(monkeypatch, _FakeService(events))
    surface = _SURFACES[name](tmp_path, tmp_path / "srv.json")
    facade = surface.responses if name == "chat" else surface.executor

    async def broken() -> None:
        raise RuntimeError("фасад не остановился")

    monkeypatch.setattr(facade, "stop_turns", broken)
    with pytest.raises(RuntimeError, match="фасад не остановился"):
        await asyncio.wait_for(surface.astop(), _CEILING_SEC)

    assert events == ["service.aclose"]


@_POSIX
@pytest.mark.parametrize("name", ["chat", "a2a"])
async def test_stop_closes_deaf_servers_within_one_budget(
    name, mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Сессии агентов с серверами, глухими к завершению, закрываются за
    один бюджет сервиса (плюс отсрочка), а не за сумму по агентам."""
    control = tmp_path / "srv.json"
    set_behavior(control, ignore_term=True, ignore_eof=True)
    surface = _SURFACES[name](tmp_path, control)
    mcp_reaper.track(surface._mcp_service)
    agent_for = _agent_for(surface, name)
    agents = 4
    for index in range(agents):
        agent = await asyncio.wait_for(
            agent_for(f"t{index}", str(tmp_path)), _CEILING_SEC
        )
        await _echo_pid(agent)
    assert len(launches(control)) == agents

    started = time.monotonic()
    await asyncio.wait_for(surface.astop(), _CEILING_SEC * 2)
    elapsed = time.monotonic() - started

    assert elapsed < _SERVICE_BUDGET_SEC + service_module._CLOSE_GRACE_SEC
    assert not any(alive(pid) for pid in launches(control))


@_POSIX
async def test_first_chat_build_without_catalog_connects_once(
    mcp_home, tmp_path, monkeypatch, capsys, mcp_reaper, mcp_owner
):
    """Холодный сервер без каталога: первая сборка ждёт своё подключение
    (тулы неизвестны), вызов идёт по той же сессии — один запуск."""
    control = tmp_path / "srv.json"
    surface = _chat(tmp_path, control)
    mcp_reaper.track(surface._mcp_service)

    agent = mcp_reaper.track(
        await asyncio.wait_for(
            surface.responses._agent_for("t", str(tmp_path)), _CEILING_SEC
        )
    )

    (pid,) = launches(control)
    assert _ECHO in agent.executor._ctx.tools_data
    assert await _echo_pid(agent) == pid
    assert launches(control) == [pid]
    assert capsys.readouterr().out == ""


@_POSIX
async def test_first_chat_build_with_catalog_connects_on_first_call(
    mcp_home, tmp_path, monkeypatch, capsys, mcp_reaper, mcp_owner
):
    """Каталог прошлого запуска процесса: сборка нового процесса ничего
    не подключает, тулы — из каталога; сервер подключается первым
    вызовом тула."""
    control = tmp_path / "srv.json"
    previous = _chat(tmp_path, control)
    mcp_reaper.track(previous._mcp_service)
    mcp_reaper.track(
        await asyncio.wait_for(
            previous.responses._agent_for("t", str(tmp_path)), _CEILING_SEC
        )
    )
    await asyncio.wait_for(previous.astop(), _CEILING_SEC)
    (old_pid,) = launches(control)
    await wait_for(lambda: old_pid in exits(control), _CEILING_SEC)

    surface = _chat(tmp_path, control)
    mcp_reaper.track(surface._mcp_service)
    agent = mcp_reaper.track(
        await asyncio.wait_for(
            surface.responses._agent_for("t", str(tmp_path)), _CEILING_SEC
        )
    )

    # Подключение не начато: статус «из каталога», не «подключается».
    status = surface._mcp_service.status(agent._mcp_consumer)
    assert status["srv"].state == "cached"
    assert launches(control) == [old_pid]
    assert _ECHO in agent.executor._ctx.tools_data
    pid = await _echo_pid(agent)
    assert launches(control) == [old_pid, pid]
    assert capsys.readouterr().out == ""


@_POSIX
@pytest.mark.parametrize("name", ["chat", "a2a"])
async def test_eviction_closes_agent_not_service(
    name, mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    surface = _SURFACES[name](tmp_path, control)
    service = mcp_reaper.track(surface._mcp_service)
    monkeypatch.setattr(_CACHES[name], "MAX_LIVE_AGENTS", 1)
    agent_for = _agent_for(surface, name)

    first = mcp_reaper.track(
        await asyncio.wait_for(agent_for("a", str(tmp_path)), _CEILING_SEC)
    )
    first_pid = await _echo_pid(first)
    second = mcp_reaper.track(
        await asyncio.wait_for(agent_for("b", str(tmp_path)), _CEILING_SEC)
    )

    assert first.closed
    await wait_for(lambda: first_pid in exits(control), _CEILING_SEC)
    assert not service.closed
    # Сервис не копит закрытых потребителей.
    assert service._consumers == {second._mcp_consumer}
    second_pid = await _echo_pid(second)
    assert second_pid != first_pid
    assert alive(second_pid)


class _EchoTurnAgent:
    """Настоящий агент сервиса; ход вместо модели вызывает ``echo``."""

    def __init__(self, agent: AsyncAgent) -> None:
        self.agent = agent
        self.executor = agent.executor
        self.pids: list[int] = []
        self.last_answer = "готово"

    async def stream(self, _prompt, **_kwargs):
        self.pids.append(await _echo_pid(self.agent))
        yield ("answer", self.last_answer)

    def stop(self) -> None:
        return None

    async def aclose(self) -> None:
        await self.agent.aclose()


@_POSIX
async def test_completions_request_leaves_no_server_process(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Агент на запрос: после ответа процесс его сервера завершён, сервис
    жив и обслуживает следующий запрос."""
    control = tmp_path / "srv.json"
    surface = _chat(tmp_path, control)
    service = mcp_reaper.track(surface._mcp_service)
    real_factory = surface.completions._agent_factory
    made: list[_EchoTurnAgent] = []

    async def factory(settings, **kwargs):
        agent = mcp_reaper.track(await real_factory(settings, **kwargs))
        made.append(_EchoTurnAgent(agent))
        return made[-1]

    surface.completions._agent_factory = factory
    body = {"model": "GC-UA", "messages": [{"role": "user", "content": "x"}]}
    for _ in range(2):
        run = await asyncio.wait_for(
            surface.completions.open(body), _CEILING_SEC
        )
        await asyncio.wait_for(run.done.wait(), _CEILING_SEC)
        (pid,) = made[-1].pids
        await wait_for(lambda pid=pid: pid in exits(control), _CEILING_SEC)
        assert made[-1].agent.closed

    pids = [turn.pids[0] for turn in made]
    assert pids[0] != pids[1]
    assert launches(control) == pids
    assert not service.closed
    assert not service._consumers


def _attempting(agent) -> bool:
    """У потребителя агента идёт своя попытка подключения."""
    connects = agent._mcp_consumer._connections._connects
    return any(not task.done() for task in connects.values())


@_POSIX
async def test_hung_server_does_not_launch_on_every_completions_request(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Сервер без каталога не отвечает: агент на запрос, закрытый посреди
    своей попытки, держит правило 30 с у записи. Запросы подряд не
    запускают сервер каждый раз; через 30 с — одна новая попытка."""
    clock = FakeClock()
    monkeypatch.setattr(
        chat_surface_mod,
        "McpService",
        functools.partial(McpService, clock=clock.now, sleep=clock.sleep),
    )
    control = tmp_path / "srv.json"
    set_behavior(control, start_delay=20)
    settings = {**_settings(control), "mcp.meta.discovery_timeout_sec": 1}
    surface = ChatSurface(runtime(tmp_path, **settings))
    mcp_reaper.track(surface._mcp_service)

    async def build():
        return mcp_reaper.track(
            await asyncio.wait_for(
                surface.completions._agent_for("t", str(tmp_path)),
                _CEILING_SEC,
            )
        )

    async def request() -> None:
        """Запрос без вызова тула: агент собран и после ответа закрыт."""
        count = len(launches(control))
        agent = await build()
        if _attempting(agent):
            # Попытка начата этим запросом: дождаться её процесса — цены.
            await wait_for(
                lambda: len(launches(control)) > count, _CEILING_SEC
            )
        await asyncio.wait_for(agent.aclose(), _CEILING_SEC)

    # Первая сборка ждёт срок; попытка кончается отказом по сроку.
    first = await build()
    (entry,) = surface._mcp_service._entries.values()
    await wait_for(lambda: entry.last_failure is not None, _CEILING_SEC)
    await asyncio.wait_for(first.aclose(), _CEILING_SEC)
    counts = [len(launches(control))]
    clock.advance(30)
    for _ in range(3):
        await request()
        counts.append(len(launches(control)))
    clock.advance(30)
    await request()
    counts.append(len(launches(control)))

    assert counts == [1, 2, 2, 2, 3]


@_POSIX
async def test_aborted_request_does_not_block_healthy_server(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Каталог есть, сервер здоров, но стартует медленно: запрос оборван
    (агент закрыт), пока первый вызов ждал подключения. Следующий запрос
    подключается сразу, и его вызов проходит."""
    control = tmp_path / "srv.json"
    previous = _chat(tmp_path, control)
    mcp_reaper.track(previous._mcp_service)
    mcp_reaper.track(
        await asyncio.wait_for(
            previous.completions._agent_for("t", str(tmp_path)),
            _CEILING_SEC,
        )
    )
    await asyncio.wait_for(previous.astop(), _CEILING_SEC)
    set_behavior(control, start_delay=2)
    surface = _chat(tmp_path, control)
    service = mcp_reaper.track(surface._mcp_service)

    async def build():
        return mcp_reaper.track(
            await asyncio.wait_for(
                surface.completions._agent_for("t", str(tmp_path)),
                _CEILING_SEC,
            )
        )

    aborted = await build()
    call = asyncio.ensure_future(_echo_pid(aborted))
    await wait_for(lambda: len(launches(control)) == 2, _CEILING_SEC)
    await asyncio.wait_for(aborted.aclose(), _CEILING_SEC)
    await asyncio.wait({call}, timeout=_CEILING_SEC)
    # Исход оборванного вызова неважен; забираем его, чтобы не висел.
    assert call.cancelled() or call.exception() or call.result()
    (entry,) = service._entries.values()
    assert entry.interrupted_at is not None

    pid = await _echo_pid(await build())

    assert launches(control)[-1] == pid
    assert alive(pid)


@_POSIX
async def test_completions_open_failure_closes_agent(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Отказ после создания агента (чтение хвоста треда) закрывает агента:
    его потребитель не остаётся у сервиса, процесс сервера выходит."""
    control = tmp_path / "srv.json"
    surface = _chat(tmp_path, control)
    service = mcp_reaper.track(surface._mcp_service)
    real_factory = surface.completions._agent_factory
    made: list[AsyncAgent] = []

    async def factory(settings, **kwargs):
        made.append(mcp_reaper.track(await real_factory(settings, **kwargs)))
        return made[-1]

    async def broken_start(*_args, **_kwargs):
        raise RuntimeError("хвост треда не прочитан")

    surface.completions._agent_factory = factory
    monkeypatch.setattr(completions_mod.ToolFlow, "start", broken_start)
    body = {"model": "GC-UA", "messages": [{"role": "user", "content": "x"}]}

    with pytest.raises(RuntimeError, match="хвост треда не прочитан"):
        await asyncio.wait_for(surface.completions.open(body), _CEILING_SEC)

    (agent,) = made
    assert agent.closed
    assert not service._consumers
    (pid,) = launches(control)
    await wait_for(lambda: pid in exits(control), _CEILING_SEC)


def _server_stop_tools() -> list[str]:
    cfg = yaml.safe_load(
        (REPO / "demo_config_server.yml").read_text(encoding="utf-8")
    )
    return cfg["webhook"]["agent_overrides"]["agent.stop_tools"]


@_POSIX
@pytest.mark.parametrize("name", ["chat", "a2a"])
async def test_server_stop_tools_hold_on_service_path(
    name, mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """``agent_overrides.stop_tools`` сервера режут набор и у агента с
    потребителем — при сборке и при пересборке на входе в ход."""
    stop = _server_stop_tools()
    config = {"agent_overrides": {"agent.stop_tools": stop}}
    surface = _SURFACES[name](tmp_path, tmp_path / "srv.json", config)
    mcp_reaper.track(surface._mcp_service)
    agent = mcp_reaper.track(
        await asyncio.wait_for(
            _agent_for(surface, name)("t", str(tmp_path)), _CEILING_SEC
        )
    )
    ctx = agent.executor._ctx

    assert _ECHO in ctx.tools_data
    assert not set(stop) & set(ctx.tools_data)
    await asyncio.wait_for(arefresh_context_tools(ctx), _CEILING_SEC)
    assert _ECHO in ctx.tools_data
    assert not set(stop) & set(ctx.tools_data)
