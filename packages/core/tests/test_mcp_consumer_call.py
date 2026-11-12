"""Исполнитель вызова тула потребителя сервиса MCP (спека §5.4, §5.8).

Вызов идёт по сессии потребителя, под его владельцем и слотами: ждёт своё
подключение, подключает сервер при первом вызове по правилу 30 с. Потеря
сессии, смерть сервера, истёкший срок, «сервер отключён» и «сервис
остановлен» доходят до модели ошибкой тула. Вызовы — через обёртку тула,
собранную из среза потребителя так же, как это делает сборка контекста.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import httpx
import pytest
from gc_core.config_modules.field_meta import plain_dump
from gc_core.execution.tool_call_context import bind_tool_call
from gc_core.mcp_session_owner import current_owner
from gc_core.tools.connect_mcp import abandonable
from gc_core.tools.connect_mcp import consumer_executor as executor_module
from gc_core.tools.connect_mcp.catalog import write_catalog
from gc_core.tools.connect_mcp.consumer_call import (
    SERVER_GONE_TEXT,
    SESSION_LOST_TEXT,
)
from gc_core.tools.connect_mcp.consumer_status import (
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_FAILED,
)
from gc_core.tools.connect_mcp.effective import effective_mcp_servers
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
)
from gc_core.tools.connect_mcp.registry import mcp_tool_full_name
from gc_core.tools.connect_mcp.server_identity import SESSION_KEY_FIELD
from gc_core.tools.connect_mcp.service import McpService
from gc_core.tools.connect_mcp.service_records import SERVICE_CLOSED_TEXT
from gc_core.tools.connect_mcp.stream_client import McpToolCallError
from gc_core.tools.connect_mcp.tool_wrapper import MCPToolWrapper
from gc_core.user_stop import UserStop, UserStopInterrupt, bind_user_stop
from gc_testkit.mcp import (
    BACKEND_ECHO_TEXT,
    FakeClock,
    IsolatedBackend,
    alive,
    events,
    exits,
    free_port,
    launches,
    set_behavior,
    stdio_cfg,
    stream_server,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.settings import make_settings

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
_ECHO = mcp_tool_full_name("a", "echo")
_NAP = mcp_tool_full_name("a", "nap")
_ACTION = mcp_tool_full_name("a", "action")


def _conn(consumer):
    """Соединения потребителя — владелец его состояния."""
    return consumer._connections


def _settings(servers: dict, *, discovery: int = 30):
    return make_settings(
        {
            "mcp": {
                "servers": servers,
                "meta": {"discovery_timeout_sec": discovery},
            }
        }
    )


def _seed(settings, key: str = "a") -> None:
    """Каталог тулов тестового сервера со схемами аргументов — как его
    запишет подключение (``seed_catalog`` пишет только имена)."""
    cfg = plain_dump(effective_mcp_servers(settings)[key], by_alias=True)
    schemas = {
        "echo": {"text": {"type": "string"}},
        "nap": {"seconds": {"type": "number"}},
    }
    tools = [
        {
            "name": name,
            "inputSchema": {"type": "object", "properties": props},
        }
        for name, props in schemas.items()
    ]
    write_catalog(key, cfg, tools)


async def _ready(reaper, service, settings, label="c", **kwargs):
    """Потребитель, сверивший набор с ``settings``."""
    consumer = reaper.track(service.open_consumer(label))
    await _ensure(consumer, settings, **kwargs)
    return consumer


async def _ensure(consumer, settings, **kwargs) -> None:
    build = consumer.next_build()
    await asyncio.wait_for(
        consumer.aensure(build, settings, **kwargs), _CEILING_SEC
    )


async def _view(consumer, settings, **kwargs):
    return await asyncio.wait_for(
        consumer.aview(settings, **kwargs), _CEILING_SEC
    )


def _wrapper(view, name: str, *, timeout: float = 20.0, os_backend=None):
    """Обёртка тула из среза — как её строит сборка контекста."""
    return MCPToolWrapper(
        name,
        view.servers_cfg,
        view.registry,
        timeout,
        default_use_retry=False,
        os_backend=os_backend,
        caller=view.caller,
    )


def _payload(envelope: str) -> dict:
    """Ответ тестового сервера из конверта результата вызова."""
    result = json.loads(envelope)["result"]
    if isinstance(result, str):
        return json.loads(result)
    return json.loads(result["content"][0]["text"])


async def _call(view, name: str, *, timeout: float = 20.0, **arguments):
    """Вызов тула через срез; ответ тестового сервера."""
    envelope = await asyncio.wait_for(
        _wrapper(view, name, timeout=timeout).acall(**arguments),
        _CEILING_SEC,
    )
    return _payload(envelope)


async def _refused(view, name: str, **arguments) -> str:
    """Вызов, отвеченный ошибкой тула; её текст."""
    with pytest.raises(McpToolCallError) as caught:
        await _call(view, name, **arguments)
    return caught.value.error.message


def _entry(consumer, key: str = "a"):
    return consumer.servers()[key]


def _status(consumer, key: str = "a"):
    return consumer.status_of(_entry(consumer, key))


def _calls_sent(control) -> int:
    return sum(e.startswith("req tools/call") for e in events(control))


def _in_flight(control) -> bool:
    return any(line.startswith("req tools/call") for line in events(control))


async def _settled(task: asyncio.Task):
    """Дождаться задачи вызова; её исход — исключение или результат."""
    done, _ = await asyncio.wait({task}, timeout=_CEILING_SEC)
    assert done, "вызов не завершился"
    return task


def _count_session_binds(monkeypatch) -> list[str]:
    """Каждое обращение вызова к реестру постоянных stdio-сессий."""
    binds: list[str] = []
    real = PersistentStdioRegistry.get_or_create

    def counting(self, server_key, server_cfg):
        binds.append(server_key)
        return real(self, server_key, server_cfg)

    monkeypatch.setattr(PersistentStdioRegistry, "get_or_create", counting)
    return binds


def _raise_on_call(monkeypatch, make_exc) -> None:
    """Вызов транспорта кончается исключением ``make_exc()``."""

    async def raising(**kwargs):
        raise make_exc()

    monkeypatch.setattr(executor_module, "acall_mcp_tool", raising)


def _stop_gate() -> UserStop:
    gate = UserStop()
    gate.prepare_run(asyncio.get_running_loop(), None)
    return gate


def _call_under_stop(gate: UserStop, view, name: str, **arguments):
    async def work():
        with bind_user_stop(gate), bind_tool_call(name, "call-1"):
            return await _wrapper(view, name).acall(**arguments)

    return asyncio.create_task(work())


async def test_call_waits_own_connect_and_uses_its_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=1.0)
    settings = _settings({"a": stdio_cfg(control)})
    _seed(settings)
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)

    out = await _call(view, _ECHO, text="hi")

    assert out["text"] == "hi"
    assert launches(control) == [out["pid"]]
    assert _conn(consumer).has_session(_entry(consumer))


async def test_lazy_first_call_connects_and_runs(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    _seed(settings)
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    assert launches(control) == []

    out = await _call(view, _ECHO, text="hi")

    assert out["text"] == "hi"
    assert launches(control) == [out["pid"]]
    assert _status(consumer).state == STATE_CONNECTED


async def test_recent_failure_is_tool_error_old_failure_reconnects(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    _seed(settings)
    service = McpService(connect_on_start=False, clock=clock.now)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    set_behavior(control, fail_start=True)
    entry = _entry(consumer)
    ok = await asyncio.wait_for(
        _conn(consumer).start_connect(entry), _CEILING_SEC
    )
    assert ok is False
    started = len(launches(control))

    text = await _refused(view, _ECHO, text="hi")

    assert text == f"сервер недоступен: {entry.last_failure.reason}"
    assert len(launches(control)) == started

    set_behavior(control, fail_start=False)
    clock.advance(30)
    out = await _call(view, _ECHO, text="hi")

    assert out["text"] == "hi"
    assert _status(consumer).state == STATE_CONNECTED


async def test_stop_cancels_the_wait(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=3.0)
    settings = _settings({"a": stdio_cfg(control)})
    _seed(settings)
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    gate = _stop_gate()

    task = _call_under_stop(gate, view, _ECHO, text="hi")
    await wait_for(lambda: bool(_conn(consumer)._connects), _CEILING_SEC)
    gate.request()
    await _settled(task)

    # Стоп снял ожидание, пока попытка ещё шла (сервер отвечает через 3 с).
    assert _status(consumer).state == STATE_CONNECTING
    with pytest.raises(UserStopInterrupt):
        task.result()
    await wait_for(
        lambda: _status(consumer).state == STATE_CONNECTED, _CEILING_SEC
    )
    assert len(launches(control)) == 1


async def test_argument_error_keeps_status_and_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    first = await _call(view, _ECHO, text="hi")

    with pytest.raises(ValueError):
        await _call(view, _ECHO, text=7)

    assert _status(consumer).state == STATE_CONNECTED
    second = await _call(view, _ECHO, text="hi")
    assert second["pid"] == first["pid"]
    assert launches(control) == [first["pid"]]


@pytest.mark.parametrize("change", ["removed", "changed"])
async def test_removed_and_changed_server_refused_from_old_view(
    change, mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    cfg = stdio_cfg(control)
    settings = _settings({"a": cfg})
    _seed(settings)
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    servers = (
        {}
        if change == "removed"
        else {"a": {**cfg, "args": [*cfg["args"], "--x"]}}
    )
    await _ensure(consumer, _settings(servers))
    started = len(launches(control))

    text = await _refused(view, _ECHO, text="hi")

    assert text == SERVER_GONE_TEXT
    assert len(launches(control)) == started


async def test_vanished_tool_returns_server_error(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    # Тестовый сервер читает список тулов на каждом запросе: перезапуск
    # процесса для смены списка не нужен.
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    [pid] = launches(control)
    set_behavior(control, tools=["echo"])

    envelope = await asyncio.wait_for(
        _wrapper(view, _NAP).acall(seconds=0), _CEILING_SEC
    )
    # Постоянный stdio отдаёт ответ JSON-RPC с ошибкой как есть.
    assert json.loads(envelope)["result"]["error"] == {
        "code": -32602,
        "message": "unknown tool nap",
    }

    assert _status(consumer).state == STATE_CONNECTED
    assert launches(control) == [pid]


async def test_cancel_without_stop_closes_stdio_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    [old] = launches(control)

    task = asyncio.create_task(_wrapper(view, _NAP).acall(seconds=30))
    await wait_for(lambda: _in_flight(control), _CEILING_SEC)
    task.cancel()
    await _settled(task)
    assert task.cancelled()

    out = await _call(view, _ECHO, text="hi")
    assert out["pid"] != old
    await wait_for(lambda: old in exits(control), _CEILING_SEC)


async def test_cancel_with_stop_keeps_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    [pid] = launches(control)
    gate = _stop_gate()

    task = _call_under_stop(gate, view, _NAP, seconds=30)
    await wait_for(lambda: _in_flight(control), _CEILING_SEC)
    gate.request()
    await _settled(task)
    with pytest.raises(UserStopInterrupt):
        task.result()

    out = await asyncio.wait_for(_wrapper(view, _ECHO).acall(text="hi"), 5)
    assert _payload(out)["pid"] == pid
    assert _status(consumer).state == STATE_CONNECTED


async def test_nine_calls_of_two_consumers_do_not_block(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    controls = [tmp_path / f"s{i}.json" for i in range(8)]
    many = _settings(
        {f"s{i}": stdio_cfg(control) for i, control in enumerate(controls)}
    )
    lone = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    service = McpService(connect_on_start=True)
    first = await _ready(mcp_reaper, service, many, "A")
    second = await _ready(mcp_reaper, service, lone, "B")
    view_a = await _view(first, many)
    view_b = await _view(second, lone)

    naps = [
        asyncio.create_task(
            _wrapper(view_a, mcp_tool_full_name(f"s{i}", "nap")).acall(
                seconds=60
            )
        )
        for i in range(8)
    ]
    try:
        await wait_for(
            lambda: all(_in_flight(c) for c in controls), _CEILING_SEC
        )
        out = await asyncio.wait_for(
            _wrapper(view_b, _ECHO).acall(text="hi"), 10
        )
        assert _payload(out)["text"] == "hi"
        assert not any(task.done() for task in naps)
    finally:
        for task in naps:
            task.cancel()
        await asyncio.wait_for(
            asyncio.gather(*naps, return_exceptions=True), _CEILING_SEC
        )


async def test_transport_failure_is_own_failure(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    first = await _ready(mcp_reaper, service, settings, "A")
    second = await _ready(mcp_reaper, service, settings, "B")
    view_a = await _view(first, settings)
    view_b = await _view(second, settings)
    pid_a = (await _call(view_a, _ECHO, text="a"))["pid"]
    pid_b = (await _call(view_b, _ECHO, text="b"))["pid"]
    assert pid_a != pid_b

    os.kill(pid_a, signal.SIGKILL)
    await wait_for(lambda: not alive(pid_a), _CEILING_SEC)
    set_behavior(control, fail_start=True)
    text = await _refused(view_a, _ECHO, text="a")

    status = _status(first)
    assert status.state == STATE_FAILED
    assert text == status.reason
    assert not _conn(first).has_session(_entry(first))
    assert _status(second).state == STATE_CONNECTED
    out = await _call(view_b, _ECHO, text="b")
    assert out["pid"] == pid_b


async def test_network_failure_reconnects_in_background(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    port = free_port()
    pause = asyncio.Event()

    async def sleep(sec: float) -> None:
        await pause.wait()

    service = McpService(connect_on_start=True, sleep=sleep)
    server = stream_server(tmp_path, "stream_http", port=port)
    network, _ = await server.__aenter__()
    try:
        settings = _settings({"a": network})
        consumer = await _ready(mcp_reaper, service, settings)
        view = await _view(consumer, settings)
        assert "count" in await _call(view, _ACTION)
    finally:
        await server.__aexit__(None, None, None)

    text = await _refused(view, _ACTION)
    assert text == _entry(consumer).last_failure.reason
    # Фоновая попытка сразу после сбоя тоже не удаётся; серия ждёт паузы.
    await wait_for(
        lambda: _status(consumer).state == STATE_FAILED, _CEILING_SEC
    )

    async with stream_server(tmp_path, "stream_http", port=port):
        pause.set()
        await wait_for(
            lambda: _status(consumer).state == STATE_CONNECTED,
            _CEILING_SEC,
        )
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)


async def test_call_timeout_keeps_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    [pid] = launches(control)

    text = await _refused(view, _NAP, timeout=1, seconds=30)

    assert text == "сервер не ответил за 1 с"
    # Серверу ушла отмена запроса (§5.8 п.4), сессия осталась.
    await wait_for(
        lambda: any(e.startswith("cancelled ") for e in events(control)),
        _CEILING_SEC,
    )
    assert _status(consumer).state == STATE_CONNECTED
    assert (await _call(view, _ECHO, text="hi"))["pid"] == pid


async def test_call_started_before_removal_closes_session_after(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    entry = _entry(consumer)
    [pid] = launches(control)

    task = asyncio.create_task(_wrapper(view, _NAP).acall(seconds=5))
    await wait_for(lambda: _in_flight(control), _CEILING_SEC)
    await _ensure(consumer, _settings({}))
    await _settled(task)

    with pytest.raises(McpToolCallError) as caught:
        task.result()
    assert caught.value.error.message == SERVER_GONE_TEXT
    await wait_for(lambda: pid in exits(control), _CEILING_SEC)
    assert entry.last_failure is None


async def test_closed_consumer_refuses_calls(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    _seed(settings)
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    text = await _refused(view, _ECHO, text="hi")

    assert text == SERVICE_CLOSED_TEXT
    assert launches(control) == []


async def test_consumer_close_cancels_inflight(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    [pid] = launches(control)

    task = asyncio.create_task(_wrapper(view, _NAP).acall(seconds=30))
    await wait_for(lambda: _in_flight(control), _CEILING_SEC)
    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)
    await _settled(task)

    assert not task.cancelled()
    with pytest.raises(McpToolCallError) as caught:
        task.result()
    assert caught.value.error.message == SERVICE_CLOSED_TEXT
    await wait_for(lambda: pid in exits(control), _CEILING_SEC)


_BACKEND_ECHO = {
    "name": "echo",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    },
}


async def test_backend_side_tool_goes_straight(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings(
        {"a": {**stdio_cfg(control), "execute_on": "os_backend"}}
    )
    backend = IsolatedBackend()
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings, os_backend=backend)
    view = await _view(consumer, settings, os_backend=backend)
    assert SESSION_KEY_FIELD not in view.servers_cfg["a"]

    envelope = await asyncio.wait_for(
        _wrapper(view, _ECHO, os_backend=backend).acall(text="hi"),
        _CEILING_SEC,
    )

    assert _payload(envelope)["text"] == BACKEND_ECHO_TEXT
    assert backend.methods == ["tools/list", "tools/call"]
    assert launches(control) == []


async def test_connect_wait_is_bounded_by_discovery_timeout(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    # Подключение, которое само не кончается (ждёт закрытия прежней
    # сессии, зависло в транспорте), не держит вызов дольше срока.
    hang = asyncio.Event()

    async def stuck(entry):
        await hang.wait()

    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)}, discovery=1)
    _seed(settings)
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    monkeypatch.setattr(_conn(consumer), "_alist_and_record", stuck)

    text = await _refused(view, _ECHO, text="hi")

    assert text == "сервер недоступен: подключение не завершилось за 1 с"
    assert _conn(consumer)._connects


async def test_sibling_cancel_close_is_not_a_failure(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    # Второй вызов стоит за первым на той же сессии; отмена первого без
    # стопа закрывает сессию — это не отказ сервера.
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    binds = _count_session_binds(monkeypatch)

    nap = asyncio.create_task(_wrapper(view, _NAP).acall(seconds=30))
    await wait_for(lambda: _in_flight(control), _CEILING_SEC)
    echo = asyncio.create_task(_wrapper(view, _ECHO).acall(text="hi"))
    # Второй вызов взял ту же сессию и встаёт на её замок.
    await wait_for(lambda: len(binds) == 2, _CEILING_SEC)
    nap.cancel()
    await _settled(nap)
    await _settled(echo)

    with pytest.raises(McpToolCallError) as caught:
        echo.result()
    assert caught.value.error.message == SESSION_LOST_TEXT
    entry = _entry(consumer)
    assert entry.last_failure is None
    assert _status(consumer).state != STATE_FAILED
    assert (await _call(view, _ECHO, text="hi"))["text"] == "hi"


async def test_call_runs_under_consumer_owner_and_slots(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    seen: list[tuple[str, object]] = []
    real = executor_module.acall_mcp_tool

    async def spy(**kwargs):
        seen.append((current_owner(), abandonable._call_slots.get()))
        return await real(**kwargs)

    monkeypatch.setattr(executor_module, "acall_mcp_tool", spy)
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)

    await _call(view, _ECHO, text="hi")

    assert seen == [(consumer.owner, _conn(consumer).slots)]


_OUTCOMES = {
    "not-running": (
        lambda: RuntimeError("persistent stdio MCP process is not running"),
        "failure",
    ),
    "mixed-group": (
        lambda: ExceptionGroup(
            "g", [httpx.ConnectError("refused"), RuntimeError("x")]
        ),
        "failure",
    ),
    "closed-resource": (lambda: anyio.ClosedResourceError(), "failure"),
    "connect-timeout": (lambda: httpx.ConnectTimeout("slow"), "failure"),
    "timeout-group": (
        lambda: ExceptionGroup(
            "g", [httpx.ReadTimeout("read"), RuntimeError("x")]
        ),
        "timeout",
    ),
    "value-group": (
        lambda: ExceptionGroup("g", [ValueError("bad")]),
        "pass",
    ),
    "cancel-group": (
        lambda: BaseExceptionGroup(
            "g", [asyncio.CancelledError(), httpx.ConnectError("x")]
        ),
        "failure",
    ),
}


@pytest.mark.parametrize("case", list(_OUTCOMES))
async def test_unlisted_exception_is_connection_failure(
    case, mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    # Как есть уходят только перечисленные исходы; всё прочее — отказ
    # связи с причиной без сырого текста группы.
    make_exc, kind = _OUTCOMES[case]
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    entry = _entry(consumer)
    _raise_on_call(monkeypatch, make_exc)

    if kind == "pass":
        with pytest.raises(ValueError, match="bad"):
            await _call(view, _ECHO, text="hi")
        assert _status(consumer).state == STATE_CONNECTED
        return
    text = await _refused(view, _ECHO, text="hi")

    assert "TaskGroup" not in text and "unhandled" not in text
    if kind == "timeout":
        assert text == "сервер не ответил за 20 с"
        assert _status(consumer).state == STATE_CONNECTED
        assert entry.last_failure is None
        return
    assert text == entry.last_failure.reason
    assert _status(consumer).state == STATE_FAILED
    assert not _conn(consumer).has_session(entry)


def _http_handler(state: dict):
    """Легаси-http сервер: подключение удаётся, ``tools/call`` по флагу
    отвечает 401."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            method = body.get("method")
            if method == "tools/call" and state["fail"]:
                self.send_response(401)
                self.end_headers()
                return
            if "id" not in body:
                self.send_response(202)
                self.end_headers()
                return
            self._reply(body["id"], _http_result(method))

        def _reply(self, rid, result) -> None:
            data = json.dumps(
                {"jsonrpc": "2.0", "id": rid, "result": result}
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("mcp-session-id", "s1")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _http_result(method: str) -> dict:
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "t", "version": "1"},
        }
    if method == "tools/list":
        return {"tools": [_BACKEND_ECHO]}
    return {"content": [{"type": "text", "text": json.dumps({"ok": 1})}]}


async def test_legacy_http_401_on_call_is_failure_without_secret(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    state = {"fail": False}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _http_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/mcp?apikey=URLSECRET123"
        settings = _settings({"a": {"transport": "http", "url": url}})
        # «При первом вызове»: без фоновых повторов статус отказа виден.
        service = McpService(connect_on_start=False)
        consumer = await _ready(mcp_reaper, service, settings)
        view = await _view(consumer, settings)
        assert await _call(view, _ECHO, text="hi") == {"ok": 1}
        state["fail"] = True

        text = await _refused(view, _ECHO, text="hi")

        entry = _entry(consumer)
        assert text == "сервер ответил 401 Unauthorized"
        assert text == entry.last_failure.reason
        assert entry.last_failure.code == "auth"
        assert _status(consumer).state == STATE_FAILED
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


async def test_stream_stdio_death_mid_call_is_own_failure(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    control = tmp_path / "a.json"
    settings = _settings(
        {"a": {**stdio_cfg(control), "transport": "stream_stdio"}}
    )
    service = McpService(connect_on_start=True, clock=clock.now)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    entry = _entry(consumer)
    [pid] = launches(control)

    nap = asyncio.create_task(_wrapper(view, _NAP).acall(seconds=30))
    await wait_for(lambda: _in_flight(control), _CEILING_SEC)
    os.kill(pid, signal.SIGKILL)
    await _settled(nap)

    with pytest.raises(McpToolCallError) as caught:
        nap.result()
    assert caught.value.error.message == "сервер завершился, не ответив"
    assert entry.last_failure.reason == caught.value.error.message
    assert _status(consumer).state == STATE_FAILED
    assert not _conn(consumer).has_session(entry)

    # В пределах 30 с правило не пускает: процесс заново не поднимается.
    text = await _refused(view, _ECHO, text="hi")
    assert text == f"сервер недоступен: {entry.last_failure.reason}"
    assert launches(control) == [pid]


async def test_old_call_failure_keeps_session_of_returned_server(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    # Сервер убрали и вернули с тем же отпечатком, новая сессия уже
    # открыта — и только потом приходит сбой вызова старой сессии.
    real = executor_module.acall_mcp_tool
    held, release = asyncio.Event(), asyncio.Event()

    async def late_failure(**kwargs):
        try:
            return await real(**kwargs)
        except Exception:
            held.set()
            await release.wait()
            raise

    monkeypatch.setattr(executor_module, "acall_mcp_tool", late_failure)
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    entry = _entry(consumer)
    [old] = launches(control)

    nap = asyncio.create_task(_wrapper(view, _NAP).acall(seconds=30))
    await wait_for(lambda: _in_flight(control), _CEILING_SEC)
    await _ensure(consumer, _settings({}))
    await asyncio.wait_for(held.wait(), _CEILING_SEC)
    await _ensure(consumer, settings)
    await wait_for(lambda: _conn(consumer).has_session(entry), _CEILING_SEC)
    new = launches(control)[-1]
    release.set()
    await _settled(nap)

    with pytest.raises(McpToolCallError) as caught:
        nap.result()
    assert caught.value.error.message == SESSION_LOST_TEXT
    assert new != old and alive(new)
    assert entry.last_failure is None
    assert _status(consumer).state == STATE_CONNECTED
    assert (await _call(view, _ECHO, text="hi"))["pid"] == new


async def test_foreign_cancel_of_call_is_not_service_stop(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    # Задачу вызова снял транспорт (область anyio), а не закрытие
    # потребителя: это потеря соединения, а не «сервис остановлен».
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    entry = _entry(consumer)
    _raise_on_call(monkeypatch, asyncio.CancelledError)

    text = await _refused(view, _ECHO, text="hi")

    assert text != SERVICE_CLOSED_TEXT
    assert text == entry.last_failure.reason
    assert _status(consumer).state == STATE_FAILED


async def test_server_returned_while_call_waits_connect(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    # Попытку снял уход сервера, сервер тут же вернули: вызов ждёт новую
    # попытку в пределах того же потолка, а не отвечает «недоступен».
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=3.0)
    settings = _settings({"a": stdio_cfg(control)})
    _seed(settings)
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)

    call = asyncio.create_task(_wrapper(view, _ECHO).acall(text="hi"))
    await wait_for(lambda: bool(_conn(consumer)._connects), _CEILING_SEC)
    set_behavior(control)
    await _ensure(consumer, _settings({}))
    await _ensure(consumer, settings)
    await _settled(call)

    assert _payload(call.result())["text"] == "hi"
    assert _status(consumer).state == STATE_CONNECTED


async def test_two_calls_to_unconnected_server_launch_once(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=1.0)
    settings = _settings({"a": stdio_cfg(control)})
    _seed(settings)
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)

    first, second = await asyncio.wait_for(
        asyncio.gather(
            _wrapper(view, _ECHO).acall(text="1"),
            _wrapper(view, _ECHO).acall(text="2"),
        ),
        _CEILING_SEC,
    )

    pids = {_payload(first)["pid"], _payload(second)["pid"]}
    assert pids == set(launches(control))
    assert len(launches(control)) == 1


async def test_close_while_call_waits_connect(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=5.0)
    settings = _settings({"a": stdio_cfg(control)})
    _seed(settings)
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)

    call = asyncio.create_task(_wrapper(view, _ECHO).acall(text="hi"))
    await wait_for(lambda: bool(launches(control)), _CEILING_SEC)
    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)
    await _settled(call)

    with pytest.raises(McpToolCallError) as caught:
        call.result()
    assert caught.value.error.message == SERVICE_CLOSED_TEXT
    pids = launches(control)
    await wait_for(lambda: not any(alive(p) for p in pids), _CEILING_SEC)


# Ошибки нашего кода, в том числе явные из поддерева ``RuntimeError``.
_CODE_ERRORS = (TypeError, NotImplementedError, RecursionError)


@pytest.mark.parametrize(
    "error", _CODE_ERRORS, ids=[e.__name__ for e in _CODE_ERRORS]
)
async def test_unexpected_type_is_logged_with_frames(
    error, mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch, caplog
):
    # Ошибка нашего кода на пути вызова: для модели и статуса — как отказ,
    # в логе — ошибка с кадрами трассировки, а не «потерял связь».
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    _raise_on_call(monkeypatch, lambda: error("bad wiring"))

    with caplog.at_level(logging.WARNING, logger=executor_module.__name__):
        text = await _refused(view, _ECHO, text="hi")

    assert text == _entry(consumer).last_failure.reason
    assert _status(consumer).state == STATE_FAILED
    [record] = [
        r for r in caplog.records if r.name == executor_module.__name__
    ]
    assert record.levelno == logging.ERROR
    message = record.getMessage()
    assert error.__name__ in message
    assert "in raising" in message


async def test_success_after_sibling_drop_keeps_connection_dropped(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    # Вызов ответил уже после того, как сосед снял соединение: ключ не
    # возвращается, следующий вызов подключается заново по правилам.
    real = executor_module.acall_mcp_tool
    held, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def late_success(**kwargs):
        calls.append(kwargs["full_tool_name"])
        result = await real(**kwargs)
        if len(calls) == 1:
            held.set()
            await release.wait()
        return result

    monkeypatch.setattr(executor_module, "acall_mcp_tool", late_success)
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    entry = _entry(consumer)

    echo = asyncio.create_task(_wrapper(view, _ECHO).acall(text="hi"))
    await asyncio.wait_for(held.wait(), _CEILING_SEC)
    nap = asyncio.create_task(_wrapper(view, _NAP).acall(seconds=30))
    await wait_for(lambda: _calls_sent(control) == 2, _CEILING_SEC)
    nap.cancel()
    await _settled(nap)
    assert not _conn(consumer).has_session(entry)
    release.set()
    await _settled(echo)

    assert _payload(echo.result())["text"] == "hi"
    assert not _conn(consumer).has_session(entry)


async def test_legacy_http_success_after_sibling_failure_confirms(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    # Сосед записал отказ, пока вызов к легаси-http шёл; вызов ответил —
    # сервер только что отвечал: связь подтверждена, правило 30 с
    # следующий вызов не держит.
    real = executor_module.acall_mcp_tool
    held, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def late_success_then_refused(**kwargs):
        calls.append(kwargs["full_tool_name"])
        if len(calls) == 2:
            raise httpx.ConnectError("refused")
        result = await real(**kwargs)
        if len(calls) == 1:
            held.set()
            await release.wait()
        return result

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _http_handler({"fail": False})
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
        settings = _settings({"a": {"transport": "http", "url": url}})
        service = McpService(connect_on_start=False, clock=FakeClock().now)
        consumer = await _ready(mcp_reaper, service, settings)
        view = await _view(consumer, settings)
        monkeypatch.setattr(
            executor_module, "acall_mcp_tool", late_success_then_refused
        )

        echo = asyncio.create_task(_wrapper(view, _ECHO).acall(text="1"))
        await asyncio.wait_for(held.wait(), _CEILING_SEC)
        await _refused(view, _ECHO, text="2")
        assert _status(consumer).state == STATE_FAILED
        release.set()
        await _settled(echo)

        assert _payload(echo.result()) == {"ok": 1}
        assert _status(consumer).state == STATE_CONNECTED
        assert await _call(view, _ECHO, text="3") == {"ok": 1}
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


async def test_cancel_group_under_stop_is_stop_not_failure(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    # Транспорт отдал группу с отменой первым листом, пока пользователь
    # просил стоп: это стоп вызова, а не отказ сервера.
    held, release = asyncio.Event(), asyncio.Event()

    async def cancelled_group(**kwargs):
        held.set()
        await release.wait()
        raise BaseExceptionGroup(
            "g", [asyncio.CancelledError(), httpx.ConnectError("x")]
        )

    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    view = await _view(consumer, settings)
    entry = _entry(consumer)
    monkeypatch.setattr(executor_module, "acall_mcp_tool", cancelled_group)
    gate = _stop_gate()

    task = _call_under_stop(gate, view, _ECHO, text="hi")
    await asyncio.wait_for(held.wait(), _CEILING_SEC)
    gate.request()
    release.set()
    await _settled(task)

    with pytest.raises(UserStopInterrupt):
        task.result()
    assert entry.last_failure is None
    assert _status(consumer).state == STATE_CONNECTED
    assert _conn(consumer).has_session(entry)
