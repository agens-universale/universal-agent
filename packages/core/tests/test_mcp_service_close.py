"""Закрытие сервиса MCP в бюджет (спека §5.12, линия 1).

``McpService.aclose`` перестаёт принимать работу и закрывает всех своих
потребителей параллельно, в общий бюджет: с ними уходят подключения,
серии повторов, идущие вызовы и сессии. Повторное и одновременное
закрытие ждут одну задачу; срок бюджета — предупреждение, не исключение.
Процессы тестовых серверов настоящие; подменяются только часы, а в тестах
прерванного закрытия — отдельные шаги закрытия сессии.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

import pytest
from gc_core.tools.connect_mcp import (
    consumer_connections as connections_module,
)
from gc_core.tools.connect_mcp import persistent_stdio, stream_client
from gc_core.tools.connect_mcp import service as service_module
from gc_core.tools.connect_mcp.client import _MCP_SESSIONS
from gc_core.tools.connect_mcp.consumer import McpConsumer
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
    PersistentStdioSession,
)
from gc_core.tools.connect_mcp.registry import mcp_tool_full_name
from gc_core.tools.connect_mcp.service import McpService
from gc_core.tools.connect_mcp.service_records import (
    SERVICE_CLOSED_TEXT,
    McpServiceClosed,
)
from gc_core.tools.connect_mcp.stream_client import McpToolCallError
from gc_core.tools.connect_mcp.tool_wrapper import MCPToolWrapper
from gc_testkit.mcp import (
    FakeClock,
    alive,
    events,
    exits,
    free_port,
    launches,
    set_behavior,
    stdio_cfg,
    wait_for,
)
from gc_testkit.mcp import fixtures as mcp_fixtures
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.settings import make_settings

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
#: Запас на закрытие после срока бюджета (снятие закрытий потребителей).
_SLACK_SEC = 3.0
_ECHO = mcp_tool_full_name("a", "echo")
_NAP = mcp_tool_full_name("a", "nap")


def _settings(servers: dict, *, discovery: int = 30):
    return make_settings(
        {
            "mcp": {
                "servers": servers,
                "meta": {"discovery_timeout_sec": discovery},
            }
        }
    )


def _stream(control) -> dict:
    return {**stdio_cfg(control), "transport": "stream_stdio"}


def _deaf(control) -> dict:
    """Сервер, глухой и к SIGTERM, и к концу stdin: закрытие ждёт
    эскалации."""
    set_behavior(control, ignore_term=True, ignore_eof=True)
    return stdio_cfg(control)


def _closed_port_cfg() -> dict:
    return {
        "transport": "stream_http",
        "url": f"http://127.0.0.1:{free_port()}",
    }


async def _ensure(consumer, settings) -> None:
    build = consumer.next_build()
    await asyncio.wait_for(consumer.aensure(build, settings), _CEILING_SEC)


async def _ready(service: McpService, settings, label: str = "c"):
    """Потребитель, сверивший набор с ``settings``; закроет его сервис."""
    consumer = service.open_consumer(label)
    await _ensure(consumer, settings)
    return consumer


async def _close(service: McpService, budget: float = 10.0) -> None:
    await asyncio.wait_for(service.aclose(budget), _CEILING_SEC)


def _wrapper(view, name: str) -> MCPToolWrapper:
    """Обёртка тула из среза — как её строит сборка контекста."""
    return MCPToolWrapper(
        name,
        view.servers_cfg,
        view.registry,
        20.0,
        default_use_retry=False,
        caller=view.caller,
    )


async def _refused(view, name: str, **arguments) -> str:
    """Вызов, отвеченный ошибкой тула; её текст."""
    with pytest.raises(McpToolCallError) as caught:
        await asyncio.wait_for(
            _wrapper(view, name).acall(**arguments), _CEILING_SEC
        )
    return caught.value.error.message


def _in_flight(control) -> bool:
    return any(line.startswith("req tools/call") for line in events(control))


def _owner_records(consumer) -> list:
    """Записи владельца потребителя во всех трёх реестрах."""
    owner = consumer.owner
    registries = (
        PersistentStdioRegistry.instance()._sessions,
        stream_client._SESSION_MANAGER._workers,
        stream_client._SESSION_MANAGER._cm_stacks,
        _MCP_SESSIONS,
    )
    return [
        key for keys in registries for key in list(keys) if key[0] == owner
    ]


def _count_consumer_closes(monkeypatch) -> list[str]:
    """Метка каждого закрытия потребителя; закрытие настоящее."""
    closes: list[str] = []
    real = McpConsumer.aclose

    async def counting(self) -> None:
        closes.append(self.label)
        await real(self)

    monkeypatch.setattr(McpConsumer, "aclose", counting)
    return closes


def _count_attempts(monkeypatch) -> list[str]:
    """Каждое обращение потребителя к серверу; транспорт настоящий."""
    calls: list[str] = []
    real = connections_module.alist_raw_tools

    async def counting(server_key, server_cfg, *, timeout):
        calls.append(server_key)
        return await real(server_key, server_cfg, timeout=timeout)

    monkeypatch.setattr(connections_module, "alist_raw_tools", counting)
    return calls


class _ParkedSleep:
    """Сон сервиса, который ждёт, пока тест сдвинет подставные часы."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.pauses: list[float] = []

    async def __call__(self, sec: float) -> None:
        self.pauses.append(sec)
        deadline = self.clock.now() + sec
        while self.clock.now() < deadline:
            await asyncio.sleep(0.01)


async def _kill_deaf(*pids: int) -> None:
    """Добить глухие серверы (это часть аварийного гашения, не закрытия) и
    дождаться фоновых задач: закрытия их сессий доигрывают в фоне, пока
    процессы живы."""
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
    await wait_for(lambda: not any(alive(p) for p in pids), _CEILING_SEC)
    others = asyncio.all_tasks() - {asyncio.current_task()}
    if others:
        _, pending = await asyncio.wait(others, timeout=_CEILING_SEC)
        assert not pending


def _cancel_session_close(monkeypatch, times: int) -> None:
    """Первые ``times`` закрытий сессии постоянного stdio проходят до
    конца и кончаются ``CancelledError`` — отменой самой сессии, как из
    её cancel scope; вызывающего никто не отменял."""
    real = PersistentStdioSession.aclose
    left = [times]

    async def cancelled_close(self) -> None:
        await real(self)
        if left[0] > 0:
            left[0] -= 1
            raise asyncio.CancelledError

    monkeypatch.setattr(PersistentStdioSession, "aclose", cancelled_close)


async def test_close_closes_all_consumers_sessions(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    plain, stream = tmp_path / "a.json", tmp_path / "b.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    first = await _ready(service, _settings({"a": stdio_cfg(plain)}), "1")
    second = await _ready(service, _settings({"b": _stream(stream)}), "2")
    pids = launches(plain) + launches(stream)
    assert len(pids) == 2

    await _close(service)

    assert not any(alive(pid) for pid in pids)
    assert first.closed and second.closed
    assert _owner_records(first) == _owner_records(second) == []
    assert not service._consumers
    assert service.closed


async def test_call_and_ensure_after_close_are_refused(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, settings)
    view = await asyncio.wait_for(consumer.aview(settings), _CEILING_SEC)
    started = launches(control)

    await _close(service)

    assert await _refused(view, _ECHO, text="hi") == SERVICE_CLOSED_TEXT
    with pytest.raises(McpServiceClosed, match="сервис MCP остановлен"):
        await _ensure(consumer, settings)
    with pytest.raises(McpServiceClosed, match="сервис MCP остановлен"):
        service.open_consumer("поздний")
    assert launches(control) == started


async def test_inflight_call_ends_with_service_stopped(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, settings)
    view = await asyncio.wait_for(consumer.aview(settings), _CEILING_SEC)
    [pid] = launches(control)

    call = asyncio.create_task(_wrapper(view, _NAP).acall(seconds=30))
    await wait_for(lambda: _in_flight(control), _CEILING_SEC)
    await _close(service)
    done, _ = await asyncio.wait({call}, timeout=_CEILING_SEC)

    assert done
    with pytest.raises(McpToolCallError) as caught:
        call.result()
    assert caught.value.error.message == SERVICE_CLOSED_TEXT
    assert pid in exits(control)


async def test_double_and_concurrent_close(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, _settings({"a": stdio_cfg(control)}))
    closes = _count_consumer_closes(monkeypatch)

    await asyncio.wait_for(
        asyncio.gather(service.aclose(), service.aclose()), _CEILING_SEC
    )
    await _close(service)

    assert service.closed
    assert consumer.closed
    # Три вызова — одна задача закрытия.
    assert closes == ["c"]


async def test_close_cancels_background_retries(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    sleep = _ParkedSleep(clock)
    attempts = _count_attempts(monkeypatch)
    settings = _settings({"srv": _closed_port_cfg()})
    service = mcp_reaper.track(
        McpService(connect_on_start=True, clock=clock.now, sleep=sleep)
    )
    consumer = await _ready(service, settings)
    await wait_for(lambda: sleep.pauses == [1], _CEILING_SEC)
    assert consumer._connections._series

    await _close(service)
    clock.advance(200)
    await asyncio.sleep(0.2)

    assert len(attempts) == 1
    assert consumer._connections._series == {}


async def test_close_during_ensure(mcp_home, tmp_path, mcp_reaper, mcp_owner):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=5)
    settings = _settings({"a": stdio_cfg(control)})
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = service.open_consumer("c")
    ensure = asyncio.create_task(
        consumer.aensure(consumer.next_build(), settings)
    )
    await wait_for(lambda: launches(control), _CEILING_SEC)
    loop = asyncio.get_running_loop()
    started = loop.time()

    await _close(service)
    done, _ = await asyncio.wait({ensure}, timeout=5)

    assert done
    assert loop.time() - started < 5
    assert isinstance(ensure.exception(), McpServiceClosed)


async def test_stop_signal_wakes_ensure_while_consumer_still_open(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=5)
    settings = _settings({"a": stdio_cfg(control)})
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = service.open_consumer("c")
    ensure = asyncio.create_task(
        consumer.aensure(consumer.next_build(), settings)
    )
    await wait_for(lambda: launches(control), _CEILING_SEC)
    real_close = McpConsumer.aclose

    async def stalled_close(self) -> None:
        # Закрытие потребителя не снимает попытку: сверку может
        # разбудить только сигнал остановки сервиса.
        await asyncio.sleep(_CEILING_SEC)

    monkeypatch.setattr(McpConsumer, "aclose", stalled_close)
    loop = asyncio.get_running_loop()
    started = loop.time()

    closing = asyncio.create_task(service.aclose(budget=0.5))
    done, _ = await asyncio.wait({ensure}, timeout=_CEILING_SEC)
    woke = loop.time() - started
    await asyncio.wait_for(closing, _CEILING_SEC)
    await asyncio.wait_for(real_close(consumer), _CEILING_SEC)

    assert done
    assert woke < 1
    assert isinstance(ensure.exception(), McpServiceClosed)


async def test_turn_hooks_are_noops_after_close(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, _settings({"a": stdio_cfg(control)}))
    started = launches(control)

    await _close(service)

    assert await asyncio.wait_for(consumer.astart_turn(), _CEILING_SEC) is (
        False
    )
    assert await asyncio.wait_for(consumer.aend_turn(), _CEILING_SEC) is None
    assert launches(control) == started


async def test_close_of_new_service_without_consumers(mcp_home):
    service = McpService(connect_on_start=True)
    assert service._loop is None

    await _close(service)
    await _close(service)

    assert service.closed
    with pytest.raises(McpServiceClosed, match="сервис MCP остановлен"):
        service.open_consumer("поздний")


async def test_unbound_service_closes_consumer_from_test_loop(
    mcp_home, mcp_reaper, mcp_owner
):
    service = mcp_reaper.track(McpService(connect_on_start=True))
    # Потребитель из потока без цикла сервис не привязывает.
    consumer = await asyncio.wait_for(
        asyncio.to_thread(service.open_consumer, "c"), _CEILING_SEC
    )
    assert service._loop is None

    await _close(service)

    assert consumer.closed
    assert not service._consumers


async def test_close_from_other_loop_is_refused(
    mcp_home, mcp_reaper, mcp_owner
):
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = service.open_consumer("c")  # привязывает к циклу теста

    with pytest.raises(RuntimeError, match="другом цикле событий"):
        await asyncio.wait_for(
            asyncio.to_thread(asyncio.run, service.aclose()), _CEILING_SEC
        )

    assert not service.closed
    await _close(service)
    assert consumer.closed


async def test_cancelled_caller_is_cancelled_and_close_goes_on(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, _settings({"a": stdio_cfg(control)}))
    [pid] = launches(control)
    closes = _count_consumer_closes(monkeypatch)

    caller = asyncio.create_task(service.aclose())
    # Отмена приходит посреди закрытия потребителя.
    await wait_for(lambda: closes == ["c"], _CEILING_SEC)
    caller.cancel()
    await asyncio.wait({caller}, timeout=_CEILING_SEC)
    await _close(service)

    assert caller.cancelled()
    assert consumer.closed
    assert pid in exits(control)
    assert closes == ["c"]


async def test_session_close_cancel_is_not_callers_cancel(
    mcp_home, tmp_path, monkeypatch, caplog, mcp_reaper, mcp_owner
):
    """Реестр постоянного stdio отдаёт ``CancelledError``, если отменилось
    закрытие одной его сессии; вызывающего закрытие сервиса никто не
    отменял."""
    _cancel_session_close(monkeypatch, times=1)
    control = tmp_path / "a.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, _settings({"a": stdio_cfg(control)}))
    [pid] = launches(control)

    with caplog.at_level(logging.WARNING):
        await _close(service)

    assert service.closed and consumer.closed
    assert not alive(pid)
    assert "потребителя c не завершено" in caplog.text


async def test_consumer_close_absorbs_session_close_cancel(
    mcp_home, tmp_path, monkeypatch, caplog, mcp_reaper, mcp_owner
):
    _cancel_session_close(monkeypatch, times=1)
    control = tmp_path / "a.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, _settings({"a": stdio_cfg(control)}))
    [pid] = launches(control)

    with caplog.at_level(logging.WARNING, logger=connections_module.__name__):
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert not alive(pid)
    assert "потребителя c не завершено" in caplog.text
    # Закрытие не завершено: потребитель за сервисом, его дозакроет сервис.
    assert consumer in service._consumers
    await _close(service)
    assert not service._consumers
    assert _owner_records(consumer) == []


async def test_cancelled_consumer_close_stays_cancelled(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    deaf = tmp_path / "deaf.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, _settings({"a": _deaf(deaf)}))
    [pid] = launches(deaf)

    closing = asyncio.create_task(consumer.aclose())
    await asyncio.sleep(0.3)  # хвост ждёт эскалации глухого сервера
    closing.cancel()
    await asyncio.wait({closing}, timeout=_CEILING_SEC)

    assert closing.cancelled()
    assert consumer in service._consumers
    await _close(service)
    await _kill_deaf(pid)


async def test_interrupted_consumer_close_is_finished_by_service_close(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    # Дочистка после отмены не успевает ничего: остаток сессий остаётся в
    # реестре, как у хоста, чьи закрытия длиннее её бюджета.
    monkeypatch.setattr(persistent_stdio, "_CANCEL_DRAIN_BUDGET_SEC", 0.0)
    deaf, plain = tmp_path / "deaf.json", tmp_path / "plain.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, _settings({"deaf": _deaf(deaf)}))
    # Вторая сверка добавляет сервер: глухой закрывается первым.
    both = _settings({"deaf": stdio_cfg(deaf), "plain": stdio_cfg(plain)})
    await _ensure(consumer, both)
    [deaf_pid], [plain_pid] = launches(deaf), launches(plain)

    # Срок хоста истекает, пока глухой к SIGTERM сервер ждёт эскалации.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(consumer.aclose(), 0.3)
    assert alive(plain_pid)
    assert consumer in service._consumers

    await _close(service)

    assert not alive(plain_pid)
    assert _owner_records(consumer) == []
    assert not service._consumers
    await _kill_deaf(deaf_pid)


async def test_budget_expiry_is_a_warning_and_close_is_not_restarted(
    mcp_home, tmp_path, monkeypatch, caplog, mcp_reaper, mcp_owner
):
    deaf = tmp_path / "deaf.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, _settings({"a": _deaf(deaf)}))
    [pid] = launches(deaf)
    closes = _count_consumer_closes(monkeypatch)
    loop = asyncio.get_running_loop()
    started = loop.time()

    with caplog.at_level(logging.WARNING, logger=service_module.__name__):
        await _close(service, budget=0.5)
    elapsed = loop.time() - started
    await _close(service, budget=0.5)

    assert elapsed < 0.5 + _SLACK_SEC
    assert service.closed and consumer.closed
    assert "не уложилось в 0.5 с" in caplog.text
    assert closes == ["c"]
    await _kill_deaf(pid)


async def test_return_is_bounded_by_budget_and_grace(
    mcp_home, tmp_path, caplog, mcp_reaper, mcp_owner
):
    controls = [tmp_path / f"d{i}.json" for i in range(3)]
    servers = {f"d{i}": _deaf(c) for i, c in enumerate(controls)}
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, _settings(servers))
    pids = [pid for control in controls for pid in launches(control)]
    assert len(pids) == 3
    # d0 уходит из набора: закрытие глухого числится за потребителем, и
    # срок бюджета снимет его, пока он ждёт это закрытие; хвост закрытия
    # двух оставшихся сессий идёт потом уже без отмен.
    rest = {k: v for k, v in servers.items() if k != "d0"}
    await _ensure(consumer, _settings(rest))
    loop = asyncio.get_running_loop()
    started = loop.time()

    with caplog.at_level(logging.WARNING, logger=service_module.__name__):
        await _close(service, budget=0.5)
    elapsed = loop.time() - started

    grace = service_module._CLOSE_GRACE_SEC
    assert elapsed < 0.5 + grace + 1
    assert service.closed
    assert "не уложилось в 0.5 с" in caplog.text
    await _kill_deaf(*pids)
    # Доигравшее в фоне закрытие сообщает исход по завершении.
    assert "потребитель c закрылся не до конца: снято" in caplog.text


async def test_reaper_closes_tracked_service_with_short_budget(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    deaf = tmp_path / "deaf.json"
    service = McpService(connect_on_start=True)
    await _ready(service, _settings({"a": _deaf(deaf)}))
    [pid] = launches(deaf)
    mcp_reaper.track(service)
    loop = asyncio.get_running_loop()
    started = loop.time()

    await mcp_reaper.reap()

    budget = mcp_fixtures._SERVICE_BUDGET_SEC
    # Полный бюджет сервиса дождался бы эскалации глухого (5 с).
    assert loop.time() - started < budget + 1.5
    assert service.closed
    assert not alive(pid)
    await _kill_deaf(pid)
