"""Сервис MCP на поверхности webhook (спека §5.1, §5.2, §5.12, §6).

Поверхность создаёт сервис с подключением «при первом вызове» и без
бэкенда (бэкенд рождается в сборке прогона). Каждый прогон получает
своего потребителя: он открывается до сборки контекста и закрывается
после прогона при любом исходе и при отказе до старта прогона.
Остановка поверхности закрывает сервис после прогонов. Процессы
тестового stdio-сервера настоящие; модель не нужна: ход вызывает тул
обёрткой из набора контекста — той, что вызвал бы цикл.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from types import SimpleNamespace

import httpx
import pytest
import yaml
from gc_core.run_context import arefresh_context_tools
from gc_core.tools.connect_mcp.consumer import McpConsumer
from gc_core.tools.connect_mcp.registry import mcp_tool_full_name
from gc_core.tools.connect_mcp.service import McpService
from gc_server.webhook import surface as webhook_mod
from gc_testkit.mcp import alive, exits, launches, stdio_cfg, wait_for
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.paths import REPO
from server_harness import LOOPBACK_URL, make_webhook

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
_ECHO = mcp_tool_full_name("srv", "echo")
_NAP = mcp_tool_full_name("srv", "nap")
_POSIX = pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")


def _within(awaitable):
    return asyncio.wait_for(awaitable, _CEILING_SEC)


class _Executor:
    """Двойник ``AgentExecutor``: исход хода задаёт ``outcome``."""

    def __init__(self, ctx, outcome: str = "completed") -> None:
        self.ctx = ctx
        self.outcome = outcome
        self.entered = asyncio.Event()

    async def arun(self, messages, **_kwargs):
        assert messages == []
        self.entered.set()
        if self.outcome == "failed":
            raise RuntimeError("ход упал")
        if self.outcome == "hang":
            await asyncio.Event().wait()
        return "готово"

    async def ainbox_left(self) -> bool:
        return False

    def stop_run(self) -> None:
        return None


class _Harness(SimpleNamespace):
    """Поверхность с подставной сборкой и двойником исполнителя."""

    async def start(self, task_id: str = "t") -> None:
        await _within(self.surface._start_run("s", task_id, None))

    async def settle(self) -> None:
        tasks = self.surface.registry.live_tasks()
        await _within(asyncio.gather(*tasks, return_exceptions=True))


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    """``fail`` — сборка падает; ``gate`` — сборка ждёт; ``outcome`` —
    исход хода; ``broken_executor`` — отказ между сборкой и стартом."""
    surface = make_webhook(tmp_path)
    h = _Harness(
        surface=surface,
        builds=[],
        executors=[],
        fail=False,
        gate=None,
        outcome="completed",
        broken_executor=False,
    )

    async def build(loaded, **kwargs):
        consumer = kwargs["mcp_consumer"]
        h.builds.append(
            (consumer, consumer in surface._mcp_service._consumers)
        )
        if h.gate is not None:
            await h.gate.wait()
        if h.fail:
            raise RuntimeError("сборка упала")
        return SimpleNamespace(logger=None)

    def executor(ctx):
        if h.broken_executor:
            raise RuntimeError("исполнитель не создан")
        h.executors.append(_Executor(ctx, h.outcome))
        return h.executors[-1]

    monkeypatch.setattr(webhook_mod, "acreate_run_context", build)
    monkeypatch.setattr(webhook_mod, "AgentExecutor", executor)
    monkeypatch.setattr(webhook_mod, "build_auth_connector", lambda _c: None)
    return h


def _consumer(h) -> McpConsumer:
    (consumer, _) = h.builds[-1]
    return consumer


def test_surface_makes_first_call_service_without_backend(
    tmp_path, monkeypatch
):
    created: list[dict] = []

    class RecordingService:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(webhook_mod, "McpService", RecordingService)

    make_webhook(tmp_path)

    assert created == [{"connect_on_start": False}]


async def test_run_gets_consumer(harness):
    await harness.start()
    await harness.settle()

    ((consumer, open_at_build),) = harness.builds
    assert isinstance(consumer, McpConsumer)
    assert open_at_build
    assert consumer.label == "webhook"


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_consumer_closes_after_run_any_outcome(harness, outcome):
    harness.outcome = "hang" if outcome == "cancelled" else outcome
    await harness.start()
    run = harness.surface.registry.run_of(system="s", task_id="t")
    if outcome == "cancelled":
        await _within(harness.executors[-1].entered.wait())
        run.task.cancel()
    await harness.settle()

    service = harness.surface._mcp_service
    assert run.ending == outcome
    assert _consumer(harness).closed
    assert not service._consumers
    assert not service.closed


async def test_finish_writes_status_then_closes_consumer(harness, monkeypatch):
    events: list[str] = []
    real_close = McpConsumer.aclose

    async def marked(run):
        events.append(f"status:{run.ending}")

    async def closing(consumer):
        events.append("consumer.aclose")
        await real_close(consumer)

    monkeypatch.setattr(harness.surface, "_amark_finished", marked)
    monkeypatch.setattr(McpConsumer, "aclose", closing)

    await harness.start()
    await harness.settle()

    assert events == ["status:completed", "consumer.aclose"]


async def test_status_write_failure_still_closes_consumer(
    harness, monkeypatch, caplog
):
    async def broken(_run):
        raise RuntimeError("статус не записан")

    monkeypatch.setattr(harness.surface, "_amark_finished", broken)

    with caplog.at_level(logging.WARNING):
        await harness.start()
        await harness.settle()

    assert _consumer(harness).closed
    assert "статус не записан" in caplog.text


async def test_consumer_close_failure_keeps_run_outcome(
    harness, monkeypatch, caplog
):
    """Закрытие потребителя упало после настоящего закрытия: итог прогона
    записан, прогон завершён, отказ — в логе."""
    seen: list[str] = []
    real_close = McpConsumer.aclose

    async def marked(run):
        seen.append(run.ending)

    async def failing(consumer):
        await real_close(consumer)
        raise RuntimeError("потребитель не закрылся")

    monkeypatch.setattr(harness.surface, "_amark_finished", marked)
    monkeypatch.setattr(McpConsumer, "aclose", failing)

    with caplog.at_level(logging.WARNING):
        await harness.start()
        await harness.settle()

    registry = harness.surface.registry
    assert seen == ["completed"]
    assert not registry.is_running(system="s", task_id="t")
    assert not registry.is_finishing(system="s", task_id="t")
    assert "потребитель не закрылся" in caplog.text
    # Отказ закрытия — свой, а не «статус не записан».
    assert "status write failed" not in caplog.text


async def test_consumer_close_failure_keeps_build_error(
    harness, monkeypatch, caplog
):
    real_close = McpConsumer.aclose

    async def failing(consumer):
        await real_close(consumer)
        raise RuntimeError("потребитель не закрылся")

    monkeypatch.setattr(McpConsumer, "aclose", failing)
    harness.fail = True

    with caplog.at_level(logging.WARNING, logger=webhook_mod.__name__):
        with pytest.raises(RuntimeError, match="сборка упала"):
            await harness.start()

    assert "потребитель не закрылся" in caplog.text


async def test_failure_between_build_and_start_closes_consumer(harness):
    harness.broken_executor = True

    with pytest.raises(RuntimeError, match="исполнитель не создан"):
        await harness.start()

    assert _consumer(harness).closed
    assert not harness.surface._mcp_service._consumers
    assert not harness.surface.registry.is_running(system="s", task_id="t")


async def test_cancelled_build_closes_consumer(harness):
    harness.gate = asyncio.Event()
    start = asyncio.ensure_future(harness.surface._start_run("s", "t", None))
    await wait_for(lambda: harness.builds, _CEILING_SEC)

    start.cancel()
    await asyncio.wait({start}, timeout=_CEILING_SEC)

    assert start.cancelled()
    assert _consumer(harness).closed
    assert not harness.surface._mcp_service._consumers


def _client(surface) -> httpx.AsyncClient:
    # Упавший эндпоинт даёт 500, как у uvicorn, а не исключение в тесте.
    transport = httpx.ASGITransport(
        app=surface.app(), raise_app_exceptions=False
    )
    return httpx.AsyncClient(transport=transport, base_url=LOOPBACK_URL)


async def _post(client: httpx.AsyncClient, text: str) -> httpx.Response:
    return await _within(
        client.post(
            "/v1/tasks", json={"task_id": "t", "system": "s", "text": text}
        )
    )


async def test_failed_build_closes_consumer_and_keeps_messages(harness):
    """Сборка упала: её потребитель закрыт, сообщение осталось во входящем
    потоке, и следующий запрос задачи строит прогон."""
    harness.fail = True
    async with _client(harness.surface) as client:
        assert (await _post(client, "первый")).status_code == 500
        failed = _consumer(harness)
        harness.fail = False
        assert (await _post(client, "второй")).json()["status"] == "started"
        await harness.settle()

    record = await harness.surface.threads().store.read_stream(
        webhook_mod.ThreadKey(system="s", thread="t"), "inbox", after_seq=0
    )
    assert failed.closed
    assert len(harness.builds) == 2
    assert [json.loads(e.data["content"])["text"] for e in record] == [
        "первый",
        "второй",
    ]
    assert not harness.surface._mcp_service._consumers


async def test_stop_mid_run_closes_consumer_then_service(harness, monkeypatch):
    events: list[str] = []
    real_consumer_close = McpConsumer.aclose
    real_service_close = McpService.aclose

    async def consumer_close(consumer):
        events.append("consumer.aclose")
        await real_consumer_close(consumer)

    async def service_close(service, *args, **kwargs):
        events.append("service.aclose")
        await real_service_close(service, *args, **kwargs)

    monkeypatch.setattr(McpConsumer, "aclose", consumer_close)
    monkeypatch.setattr(McpService, "aclose", service_close)
    harness.outcome = "hang"
    await harness.start()
    run = harness.surface.registry.run_of(system="s", task_id="t")
    await _within(harness.executors[-1].entered.wait())

    await _within(harness.surface.astop())

    assert run.ending == "interrupted"
    assert _consumer(harness).closed
    assert harness.surface._mcp_service.closed
    assert events == ["consumer.aclose", "service.aclose"]


async def test_stop_closes_service(harness):
    service = harness.surface._mcp_service

    # Процесс зовёт ``astop`` дважды: из ``watch`` и из ``finally``.
    await _within(harness.surface.astop())
    await _within(harness.surface.astop())

    assert service.closed


class _FailingService:
    def __init__(self) -> None:
        self.calls = 0
        self.kills = 0

    async def aclose(self) -> None:
        self.calls += 1
        raise RuntimeError("сервис не закрылся")

    def kill_processes_now(self) -> None:
        self.kills += 1


async def test_service_close_failure_does_not_break_stop(harness, caplog):
    surface = harness.surface
    surface._mcp_service = _FailingService()
    pruner = surface._pruner = asyncio.ensure_future(asyncio.sleep(3600))

    with caplog.at_level(logging.ERROR, logger="gc_server.surface"):
        await _within(surface.astop())

    assert pruner.cancelled()
    assert surface._mcp_service.calls == 1
    assert surface._mcp_service.kills == 1
    assert "сервис не закрылся" in caplog.text


async def test_service_closes_when_stop_step_fails(harness, monkeypatch):
    async def broken() -> None:
        raise RuntimeError("обратные вызовы не закрылись")

    monkeypatch.setattr(harness.surface.callbacks, "aclose", broken)

    with pytest.raises(RuntimeError, match="обратные вызовы не закрылись"):
        await _within(harness.surface.astop())

    assert harness.surface._mcp_service.closed


# --- настоящий сервис и stdio-сервер ---------------------------------------


def _settings(control) -> dict:
    return {
        "os_backend": "local",
        "agent.model": "m",
        "mcp.servers": {"srv": stdio_cfg(control)},
        "mcp.meta.discovery_timeout_sec": 30,
        "logging.save_logs": False,
    }


async def _echo_pid(ctx) -> int:
    """Вызов ``echo`` обёрткой из набора контекста — той, что вызовет цикл."""
    tool = ctx.tools_data[_ECHO]["tool_fabric"]()
    envelope = await _within(tool.acall(text="эхо"))
    (content,) = json.loads(envelope)["result"]["content"]
    return json.loads(content["text"])["pid"]


class _EchoExecutor:
    """Ход вместо модели вызывает ``echo``; контекст собран по-настоящему."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.pids: list[int] = []

    async def arun(self, messages, **_kwargs):
        assert messages == []
        self.pids.append(await _echo_pid(self.ctx))
        return "готово"

    async def ainbox_left(self) -> bool:
        return False

    def stop_run(self) -> None:
        return None


def _echo_executors(monkeypatch) -> list[_EchoExecutor]:
    made: list[_EchoExecutor] = []

    def executor(ctx):
        made.append(_EchoExecutor(ctx))
        return made[-1]

    monkeypatch.setattr(webhook_mod, "AgentExecutor", executor)
    return made


async def _run_once(surface, task_id: str) -> None:
    await _within(surface._start_run("s", task_id, None))
    tasks = surface.registry.live_tasks()
    await _within(asyncio.gather(*tasks, return_exceptions=True))


@_POSIX
async def test_run_leaves_no_server_process(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Прогон вызывает тул по сессии своего потребителя; после прогона
    процесс сервера завершён, сервис жив, следующий прогон поднимает
    новый процесс. Потребители у сервиса не копятся."""
    control = tmp_path / "srv.json"
    surface = make_webhook(tmp_path, **_settings(control))
    service = mcp_reaper.track(surface._mcp_service)
    made = _echo_executors(monkeypatch)

    for task_id in ("a", "b"):
        await _run_once(surface, task_id)
        (pid,) = made[-1].pids
        await wait_for(lambda pid=pid: pid in exits(control), _CEILING_SEC)
        assert not service._consumers

    pids = [executor.pids[0] for executor in made]
    assert pids[0] != pids[1]
    assert launches(control) == pids
    assert not service.closed


@_POSIX
async def test_stop_during_run_stops_server_process(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    surface = make_webhook(tmp_path, **_settings(control))
    mcp_reaper.track(surface._mcp_service)
    made = _echo_executors(monkeypatch)
    entered = asyncio.Event()

    async def hang(self, messages, **_kwargs):
        self.pids.append(await _echo_pid(self.ctx))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(_EchoExecutor, "arun", hang)
    await _within(surface._start_run("s", "t", None))
    await _within(entered.wait())
    (pid,) = made[-1].pids
    assert alive(pid)

    await _within(surface.astop())

    await wait_for(lambda: pid in exits(control), _CEILING_SEC)
    assert surface._mcp_service.closed
    assert not surface._mcp_service._consumers


def _server_stop_tools() -> list[str]:
    cfg = yaml.safe_load(
        (REPO / "demo_config_server.yml").read_text(encoding="utf-8")
    )
    return cfg["webhook"]["agent_overrides"]["agent.stop_tools"]


@_POSIX
async def test_server_stop_tools_hold_on_webhook_run(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """``agent_overrides.stop_tools`` поверхности режут набор прогона и на
    пути потребителя — при сборке и при пересборке на входе в ход; тул
    MCP в списке тоже снимается."""
    stop = [*_server_stop_tools(), _NAP]
    control = tmp_path / "srv.json"
    surface = make_webhook(
        tmp_path,
        config={"webhook": {"agent_overrides": {"agent.stop_tools": stop}}},
        **_settings(control),
    )
    mcp_reaper.track(surface._mcp_service)
    seen: list[set] = []

    class Inspecting(_EchoExecutor):
        async def arun(self, messages, **_kwargs):
            seen.append(set(self.ctx.tools_data))
            await _within(arefresh_context_tools(self.ctx))
            seen.append(set(self.ctx.tools_data))
            return "готово"

    monkeypatch.setattr(webhook_mod, "AgentExecutor", Inspecting)

    await _run_once(surface, "t")

    assert len(seen) == 2
    for names in seen:
        assert _ECHO in names
        assert not set(stop) & names
