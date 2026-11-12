"""Потребитель сервиса MCP: подключение своей сессией и свой статус.

Подключение и есть сессия: список тулов идёт по тому соединению, которое
потом обслужит вызовы потребителя. Статус сервера у каждого потребителя
свой; сетевой сервер у потребителя «на старте» после отказа
переподключается серией фоновых повторов. Сроки повторов и правило 30 с
идут по подставным часам сервиса.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gc_core.config_modules.field_meta import plain_dump
from gc_core.mcp_session_owner import current_owner, use_owner
from gc_core.settings import settings_of
from gc_core.tools.connect_mcp import (
    consumer_connections as connections_module,
)
from gc_core.tools.connect_mcp import persistent_stdio, stream_client
from gc_core.tools.connect_mcp.client import _MCP_SESSIONS
from gc_core.tools.connect_mcp.consumer import RETRY_PAUSES_SEC
from gc_core.tools.connect_mcp.consumer_status import (
    STATE_CACHED,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_FAILED,
)
from gc_core.tools.connect_mcp.effective import effective_mcp_servers
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
)
from gc_core.tools.connect_mcp.server_identity import session_key
from gc_core.tools.connect_mcp.service import McpService
from gc_core.tools.connect_mcp.service_records import McpServiceClosed
from gc_core.user_stop import UserStop, bind_user_stop, current_user_stop
from gc_testkit.mcp import (
    FakeClock,
    alive,
    call_tool,
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
_INTERRUPTED = "подключение прервано"


def _conn(consumer):
    """Соединения потребителя — владелец его состояния."""
    return consumer._connections


def _settings(servers: dict, *, discovery: int = 30, **runtime):
    return make_settings(
        {
            "mcp": {
                "servers": servers,
                "meta": {"discovery_timeout_sec": discovery},
            },
            **runtime,
        }
    )


def _consumer(reaper, service: McpService, settings, label: str = "c"):
    """Потребитель с настройками сборки — сверки ещё нет."""
    consumer = reaper.track(service.open_consumer(label))
    _conn(consumer)._settings = settings
    return consumer


def _entry(service: McpService, settings, key: str = "srv"):
    """Запись сервера по определению без trace, как её заводит сверка."""
    cfg = plain_dump(effective_mcp_servers(settings)[key], by_alias=True)
    timeout = settings_of(settings).mcp.meta.discovery_timeout_sec
    return service.entry(key, cfg, discovery_timeout=timeout)


def _in_set(consumer, entry):
    """Сервер в наборе потребителя, как после сверки: серия повторов
    кончается, если сервера в наборе нет."""
    _conn(consumer)._servers[entry.key] = entry
    return entry


async def _connect(consumer, entry) -> None:
    """Подключить и дождаться; срок ожидания попытку не прерывает."""
    await asyncio.wait_for(_conn(consumer).start_connect(entry), _CEILING_SEC)


def _attempt(consumer, entry) -> asyncio.Task:
    """Сама задача попытки — её отменяют только сверка и закрытие."""
    return _conn(consumer)._connects[session_key(entry.key, entry.fingerprint)]


def _closed_port_cfg() -> dict:
    return {
        "transport": "stream_http",
        "url": f"http://127.0.0.1:{free_port()}",
    }


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


@contextlib.contextmanager
def _unauthorized_server():
    """HTTP-заглушка: на любой запрос — 401."""

    class Handler(BaseHTTPRequestHandler):
        def _deny(self):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = do_DELETE = _deny

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_connect_is_the_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    await _connect(consumer, entry)
    with use_owner(consumer.owner):
        out = await call_tool(
            "srv", _conn(consumer).wrapper_cfg(entry), "echo", {"text": "hi"}
        )

    status = consumer.status_of(entry)
    assert status.state == STATE_CONNECTED
    assert [tool["name"] for tool in status.tools] == ["echo", "nap"]
    assert launches(control) == [out["pid"]]
    assert alive(out["pid"])
    assert _conn(consumer).has_session(entry)


async def test_status_is_connecting_before_first_step(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    settings = _settings({"srv": stdio_cfg(tmp_path / "s.json")})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    waiting = _conn(consumer).start_connect(entry)
    task = _attempt(consumer, entry)

    assert consumer.status_of(entry).state == STATE_CONNECTING
    again = _conn(consumer).start_connect(entry)
    assert _attempt(consumer, entry) is task
    assert await asyncio.wait_for(waiting, _CEILING_SEC) is True
    assert await asyncio.wait_for(again, _CEILING_SEC) is True
    assert consumer.status_of(entry).state == STATE_CONNECTED


async def test_cancel_before_first_step_leaves_no_connecting(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    _conn(consumer).start_connect(entry)
    task = _attempt(consumer, entry)
    task.cancel()
    await asyncio.wait({task}, timeout=_CEILING_SEC)

    status = consumer.status_of(entry)
    assert status.state == STATE_FAILED
    assert (status.reason_code, status.reason) == ("other", _INTERRUPTED)
    assert _conn(consumer)._connects == {}
    assert launches(control) == []


async def test_connect_with_discovery_timeout_not_ten_seconds(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    set_behavior(control, start_delay=10.5)
    settings = _settings({"srv": stdio_cfg(control)}, discovery=30)
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    await _connect(consumer, entry)

    assert consumer.status_of(entry).state == STATE_CONNECTED
    assert len(launches(control)) == 1


async def test_connect_timeout_comes_from_consumer_settings(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    set_behavior(control, start_delay=5)
    service = McpService(connect_on_start=True)
    # Запись заведена другим потребителем со сроком 30 с.
    entry = _entry(service, _settings({"srv": stdio_cfg(control)}))
    consumer = _consumer(
        mcp_reaper,
        service,
        _settings({"srv": stdio_cfg(control)}, discovery=1),
    )

    await asyncio.wait_for(_conn(consumer).start_connect(entry), 4)

    status = consumer.status_of(entry)
    assert (status.state, status.reason_code) == (STATE_FAILED, "timeout")
    # Попытка завершается после закрытия остатка: процесса уже нет.
    [pid] = launches(control)
    assert not alive(pid)


async def test_empty_list_connected_missing_key_failed(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    empty, broken = tmp_path / "a.json", tmp_path / "b.json"
    set_behavior(empty, tools=[])
    set_behavior(broken, no_tools_key=True)
    settings = _settings({"a": stdio_cfg(empty), "b": stdio_cfg(broken)})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    a, b = _entry(service, settings, "a"), _entry(service, settings, "b")

    await _connect(consumer, a)
    await _connect(consumer, b)

    assert consumer.status_of(a).state == STATE_CONNECTED
    assert consumer.status_of(a).tools == ()
    status_b = consumer.status_of(b)
    assert (status_b.state, status_b.reason_code) == (STATE_FAILED, "protocol")
    [pid] = launches(broken)
    await wait_for(lambda: not alive(pid))


async def test_reason_codes_end_to_end(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    slow = tmp_path / "slow.json"
    set_behavior(slow, start_delay=5)
    with _unauthorized_server() as denied_url:
        settings = _settings(
            {
                "missing": {
                    "transport": "stdio",
                    "command": str(tmp_path / "no-such-command"),
                },
                "slow": stdio_cfg(slow),
                "closed": _closed_port_cfg(),
                "denied": {"transport": "stream_http", "url": denied_url},
            },
            discovery=1,
        )
        service = McpService(connect_on_start=False)
        consumer = _consumer(mcp_reaper, service, settings)
        keys = ["missing", "slow", "closed", "denied"]
        entries = [_entry(service, settings, key) for key in keys]

        await asyncio.wait_for(
            asyncio.gather(
                *(_conn(consumer).start_connect(e) for e in entries)
            ),
            _CEILING_SEC,
        )

    codes = [consumer.status_of(e).reason_code for e in entries]
    assert codes == ["command_not_found", "timeout", "connect_error", "auth"]
    assert {consumer.status_of(e).state for e in entries} == {STATE_FAILED}


async def test_trace_reaches_server_on_connect(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    server = {**stdio_cfg(control), "trace_header_enabled": True}
    settings = _settings({"srv": server}, trace_id="t1")
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    await _connect(consumer, entry)
    with use_owner(consumer.owner):
        out = await call_tool(
            "srv", _conn(consumer).wrapper_cfg(entry), "echo", {"text": "hi"}
        )

    assert out["trace"] == "t1"
    assert launches(control) == [out["pid"]]


async def test_failure_of_one_consumer_keeps_other_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    first = _consumer(mcp_reaper, service, settings, "a")
    second = _consumer(mcp_reaper, service, settings, "b")
    entry = _entry(service, settings)
    await _connect(first, entry)
    [pid] = launches(control)

    set_behavior(control, fail_start=True)
    await _connect(second, entry)

    assert first.status_of(entry).state == STATE_CONNECTED
    assert second.status_of(entry).state == STATE_FAILED
    with use_owner(first.owner):
        out = await call_tool(
            "srv", _conn(first).wrapper_cfg(entry), "echo", {"text": "hi"}
        )
    assert out["pid"] == pid


async def test_network_server_retries_five_times(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    retries: list[int] = []

    async def sleep(sec: float) -> None:
        retries.append(consumer.status_of(entry).retry)
        await clock.sleep(sec)

    attempts = _count_attempts(monkeypatch)
    settings = _settings({"srv": _closed_port_cfg()})
    service = McpService(connect_on_start=True, clock=clock.now, sleep=sleep)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _in_set(consumer, _entry(service, settings))

    await _connect(consumer, entry)
    await wait_for(
        lambda: (
            clock.now() >= 104
            and not _conn(consumer)._series
            and not _conn(consumer)._connects
        ),
        _CEILING_SEC,
    )

    assert retries == [1, 2, 3, 4, 5]
    assert clock.now() == 104
    assert len(attempts) == 6
    status = consumer.status_of(entry)
    assert (status.state, status.retry) == (STATE_FAILED, 0)


async def test_retry_succeeds_when_server_appears(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    port = free_port()
    async with contextlib.AsyncExitStack() as stack:

        async def sleep(sec: float) -> None:
            if sec == RETRY_PAUSES_SEC[1]:
                await stack.enter_async_context(
                    stream_server(tmp_path, "stream_http", port=port)
                )
            await clock.sleep(sec)

        attempts = _count_attempts(monkeypatch)
        settings = _settings(
            {
                "srv": {
                    "transport": "stream_http",
                    "url": f"http://127.0.0.1:{port}",
                }
            }
        )
        service = McpService(
            connect_on_start=True, clock=clock.now, sleep=sleep
        )
        consumer = _consumer(mcp_reaper, service, settings)
        entry = _in_set(consumer, _entry(service, settings))

        await _connect(consumer, entry)
        await wait_for(
            lambda: (
                consumer.status_of(entry).state == STATE_CONNECTED
                and not _conn(consumer)._series
            ),
            _CEILING_SEC,
        )

        assert len(attempts) == 3
        assert consumer.status_of(entry).retry == 0
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)


async def test_retries_stop_on_consumer_close(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    sleep = _ParkedSleep(clock)
    attempts = _count_attempts(monkeypatch)
    settings = _settings({"srv": _closed_port_cfg()})
    service = McpService(connect_on_start=True, clock=clock.now, sleep=sleep)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _in_set(consumer, _entry(service, settings))
    await _connect(consumer, entry)
    await wait_for(lambda: sleep.pauses == [1])

    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)
    clock.advance(200)
    await asyncio.sleep(0.2)

    assert len(attempts) == 1
    assert _conn(consumer)._series == {}


async def test_lazy_service_does_not_retry(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    attempts = _count_attempts(monkeypatch)
    settings = _settings({"srv": _closed_port_cfg()})
    service = McpService(
        connect_on_start=False, clock=clock.now, sleep=clock.sleep
    )
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    await _connect(consumer, entry)
    clock.advance(200)
    await asyncio.sleep(0.2)

    assert consumer.status_of(entry).state == STATE_FAILED
    assert len(attempts) == 1
    assert _conn(consumer)._series == {}


async def test_process_server_does_not_retry(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    control = tmp_path / "s.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(
        connect_on_start=True, clock=clock.now, sleep=clock.sleep
    )
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    await _connect(consumer, entry)
    clock.advance(200)
    await asyncio.sleep(0.2)

    assert consumer.status_of(entry).state == STATE_FAILED
    assert len(launches(control)) == 1
    assert _conn(consumer)._series == {}


async def test_may_connect_interval(mcp_home, tmp_path, mcp_reaper, mcp_owner):
    clock = FakeClock()
    control = tmp_path / "s.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(
        connect_on_start=True, clock=clock.now, sleep=clock.sleep
    )
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)
    await _connect(consumer, entry)

    seen = [_conn(consumer).may_connect(entry)]
    clock.advance(30)
    seen.append(_conn(consumer).may_connect(entry))
    task = _conn(consumer).start_connect(entry)
    seen.append(_conn(consumer).may_connect(entry))
    await asyncio.wait_for(task, _CEILING_SEC)

    assert seen == [False, True, False]


async def test_interrupted_attempt_counts_from_its_start(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    control = tmp_path / "s.json"
    set_behavior(control, start_delay=5)
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(
        connect_on_start=True, clock=clock.now, sleep=clock.sleep
    )
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)
    _conn(consumer).start_connect(entry)
    task = _attempt(consumer, entry)
    await wait_for(lambda: launches(control))
    clock.advance(20)

    task.cancel()
    await asyncio.wait({task}, timeout=_CEILING_SEC)
    blocked = _conn(consumer).may_connect(entry)
    clock.advance(10)

    assert blocked is False
    assert _conn(consumer).may_connect(entry) is True
    assert entry.last_failure is None


async def _failed_entry(reaper, service, settings, control, clock):
    """Запись в отказе: попытка упала на старте в момент 0, затем 30 с;
    сервер дальше стартует медленно (2 с), но поднимается."""
    set_behavior(control, fail_start=True)
    entry = _entry(service, settings)
    await _connect(_consumer(reaper, service, settings, "failed"), entry)
    assert entry.last_failure is not None
    set_behavior(control, start_delay=2)
    clock.advance(30)
    return entry


async def test_close_mid_attempt_of_failed_server_holds_interval(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Запись в отказе: попытка, прерванная закрытием потребителя, держит
    правило 30 с от своего начала — потребитель на запрос иначе поднимал
    бы отказавший сервер каждый раз. Это не отказ: отказ записи прежний,
    чужой причины у статуса нет."""
    clock = FakeClock()
    control = tmp_path / "s.json"
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(
        connect_on_start=False, clock=clock.now, sleep=clock.sleep
    )
    entry = await _failed_entry(mcp_reaper, service, settings, control, clock)
    failure = entry.last_failure
    closed = _consumer(mcp_reaper, service, settings, "a")
    _conn(closed).start_connect(entry)
    await wait_for(lambda: len(launches(control)) == 2)
    clock.advance(5)
    await asyncio.wait_for(closed.aclose(), _CEILING_SEC)
    fresh = _consumer(mcp_reaper, service, settings, "b")

    seen = [_conn(fresh).may_connect(entry)]
    clock.advance(24)
    seen.append(_conn(fresh).may_connect(entry))
    clock.advance(1)
    seen.append(_conn(fresh).may_connect(entry))

    assert seen == [False, False, True]
    assert entry.last_failure is failure
    assert fresh.status_of(entry).reason != _INTERRUPTED


@pytest.mark.parametrize("connect_on_start", [False, True])
async def test_close_mid_attempt_of_healthy_server_does_not_hold(
    connect_on_start, mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Отказов не было (как закрытая во время подключения сессия ACP или
    оборванный запрос): новый потребитель подключается сразу."""
    clock = FakeClock()
    control = tmp_path / "s.json"
    set_behavior(control, start_delay=5)
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(
        connect_on_start=connect_on_start, clock=clock.now, sleep=clock.sleep
    )
    closed = _consumer(mcp_reaper, service, settings, "a")
    entry = _entry(service, settings)
    _conn(closed).start_connect(entry)
    await wait_for(lambda: launches(control))
    clock.advance(1)
    await asyncio.wait_for(closed.aclose(), _CEILING_SEC)
    set_behavior(control)
    fresh = _consumer(mcp_reaper, service, settings, "b")

    assert entry.interrupted_at == 0
    assert _conn(fresh).may_connect(entry) is True
    await _connect(fresh, entry)
    assert fresh.status_of(entry).state == STATE_CONNECTED


async def test_success_after_interruption_lifts_shared_interval(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    control = tmp_path / "s.json"
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(
        connect_on_start=False, clock=clock.now, sleep=clock.sleep
    )
    entry = await _failed_entry(mcp_reaper, service, settings, control, clock)
    closed = _consumer(mcp_reaper, service, settings, "a")
    other = _consumer(mcp_reaper, service, settings, "b")
    _conn(closed).start_connect(entry)
    connecting = _conn(other).start_connect(entry)
    await wait_for(lambda: len(launches(control)) == 3)
    clock.advance(1)
    await asyncio.wait_for(closed.aclose(), _CEILING_SEC)
    held = _conn(_consumer(mcp_reaper, service, settings, "c")).may_connect(
        entry
    )

    assert await asyncio.wait_for(connecting, _CEILING_SEC) is True
    fresh = _consumer(mcp_reaper, service, settings, "d")

    assert held is False
    assert _conn(fresh).may_connect(entry) is True


async def test_withdrawal_by_set_does_not_mark_entry(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Сервер убрали из набора посреди попытки — запись не помечается:
    вернули — подключается сразу."""
    clock = FakeClock()
    control = tmp_path / "s.json"
    set_behavior(control, start_delay=5)
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(
        connect_on_start=False, clock=clock.now, sleep=clock.sleep
    )
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _in_set(consumer, _entry(service, settings))
    _conn(consumer).start_connect(entry)
    task = _attempt(consumer, entry)
    await wait_for(lambda: launches(control))

    _conn(consumer).leave(entry)
    await asyncio.wait({task}, timeout=_CEILING_SEC)

    assert task.cancelled()
    assert entry.interrupted_at is None
    assert _conn(consumer).may_connect(entry) is True
    fresh = _consumer(mcp_reaper, service, settings, "b")
    assert _conn(fresh).may_connect(entry) is True


async def test_success_does_not_block_reconnect(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    control = tmp_path / "s.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(
        connect_on_start=True, clock=clock.now, sleep=clock.sleep
    )
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)
    await _connect(consumer, entry)
    set_behavior(control)
    clock.advance(30)
    await _connect(consumer, entry)
    clock.advance(1)

    with use_owner(consumer.owner):
        closed = await asyncio.wait_for(
            PersistentStdioRegistry.instance().aclose_one(
                session_key(entry.key, entry.fingerprint)
            ),
            _CEILING_SEC,
        )

    assert closed is True
    assert _conn(consumer).may_connect(entry) is True


async def test_retry_status_between_attempts(mcp_home, mcp_reaper, mcp_owner):
    clock = FakeClock()
    sleep = _ParkedSleep(clock)
    settings = _settings({"srv": _closed_port_cfg()})
    service = McpService(connect_on_start=True, clock=clock.now, sleep=sleep)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _in_set(consumer, _entry(service, settings))
    await _connect(consumer, entry)

    seen = []
    for done, pause in enumerate(RETRY_PAUSES_SEC[:3], start=1):
        await wait_for(lambda: len(sleep.pauses) == done)
        status = consumer.status_of(entry)
        seen.append(
            (status.state, status.retry, dict(_conn(consumer)._connects))
        )
        clock.advance(pause)

    assert seen == [(STATE_FAILED, n, {}) for n in (1, 2, 3)]


async def test_fresh_consumer_respects_shared_failure(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    control = tmp_path / "s.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(
        connect_on_start=True, clock=clock.now, sleep=clock.sleep
    )
    first = _consumer(mcp_reaper, service, settings, "a")
    entry = _entry(service, settings)
    await _connect(first, entry)
    second = _consumer(mcp_reaper, service, settings, "b")

    blocked = _conn(second).may_connect(entry)
    status = second.status_of(entry)
    clock.advance(30)

    assert blocked is False
    assert status.state == STATE_FAILED
    assert (status.reason_code, status.reason) == (
        entry.last_failure.code,
        entry.last_failure.reason,
    )
    assert _conn(second).may_connect(entry) is True


async def test_shared_failure_before_success_does_not_block(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    control = tmp_path / "s.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(
        connect_on_start=True, clock=clock.now, sleep=clock.sleep
    )
    failed = _consumer(mcp_reaper, service, settings, "a")
    entry = _entry(service, settings)
    await _connect(failed, entry)
    set_behavior(control)
    clock.advance(1)
    await _connect(_consumer(mcp_reaper, service, settings, "b"), entry)
    fresh = _consumer(mcp_reaper, service, settings, "c")

    assert entry.last_failure is not None
    assert _conn(fresh).may_connect(entry) is True
    assert fresh.status_of(entry).state == STATE_CACHED
    assert _conn(failed).may_connect(entry) is False


async def test_background_connect_does_not_see_turn_stop(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    seen = []

    async def fake_list(server_key, server_cfg, *, timeout):
        seen.append((current_user_stop(), current_owner()))
        return [{"name": "echo"}]

    monkeypatch.setattr(connections_module, "alist_raw_tools", fake_list)
    settings = _settings({"srv": stdio_cfg(tmp_path / "s.json")})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    with bind_user_stop(UserStop()):
        task = _conn(consumer).start_connect(entry)
    await asyncio.wait_for(task, _CEILING_SEC)

    assert seen == [(None, consumer.owner)]


async def test_close_during_connect(mcp_home, tmp_path, mcp_reaper, mcp_owner):
    control = tmp_path / "s.json"
    set_behavior(control, start_delay=5)
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    _conn(consumer).start_connect(entry)
    await wait_for(lambda: launches(control))
    await asyncio.sleep(0.5)
    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert consumer.status_of(entry).state != STATE_CONNECTING
    [pid] = launches(control)
    await wait_for(lambda: not alive(pid))


async def test_close_closes_own_sessions(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)
    await _connect(consumer, entry)
    [pid] = launches(control)

    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)
    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert exits(control) == [pid]
    assert consumer.closed is True
    assert consumer not in service._consumers
    assert not _conn(consumer).has_session(entry)


async def test_server_without_persistent_session_has_no_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    async with stream_server(tmp_path, "stream_http") as (network, _):
        settings = _settings(
            {
                "local": {**stdio_cfg(control), "persistent_session": False},
                "net": network,
            }
        )
        service = McpService(connect_on_start=True)
        consumer = _consumer(mcp_reaper, service, settings)
        local = _entry(service, settings, "local")
        net = _entry(service, settings, "net")

        await _connect(consumer, local)
        await _connect(consumer, net)
        statuses = {consumer.status_of(e).state for e in (local, net)}
        sessions, ok = set(_conn(consumer)._sessions), set(_conn(consumer)._ok)
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert statuses == {STATE_CONNECTED}
    # Разовый stdio: процессы обмена завершаются вместе с ним.
    assert launches(control)
    assert not any(alive(pid) for pid in launches(control))
    assert sessions == set()
    assert ok == {session_key(e.key, e.fingerprint) for e in (local, net)}


async def test_failed_reconnect_with_live_session_is_failed(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)
    await _connect(consumer, entry)

    set_behavior(control, exit_on="tools/list")
    await _connect(consumer, entry)

    assert consumer.status_of(entry).state == STATE_FAILED
    assert not _conn(consumer).has_session(entry)


async def test_exception_in_connect_marks_failed_silently(
    mcp_home, tmp_path, monkeypatch, capsys, mcp_reaper, mcp_owner
):
    async def boom(server_key, server_cfg, *, timeout):
        raise RuntimeError("boom")

    monkeypatch.setattr(connections_module, "alist_raw_tools", boom)
    settings = _settings({"srv": stdio_cfg(tmp_path / "s.json")})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    await _connect(consumer, entry)

    status = consumer.status_of(entry)
    assert (status.state, status.reason_code) == (STATE_FAILED, "other")
    assert capsys.readouterr().out == ""


async def test_open_consumer_after_stop_is_refused(mcp_home):
    service = McpService(connect_on_start=True)
    service._stopped = True

    with pytest.raises(McpServiceClosed, match="сервис MCP остановлен"):
        service.open_consumer("late")


@pytest.fixture
def slow_spawn(monkeypatch):
    """Запуск stdio-процесса растянут: окно «сессия есть, процесса нет»."""
    real = persistent_stdio._spawn_detached_stdio_process
    spawned = []

    def spawn(cfg):
        time.sleep(0.5)
        spawned.append(real(cfg))
        return spawned[-1]

    monkeypatch.setattr(
        persistent_stdio, "_spawn_detached_stdio_process", spawn
    )
    return spawned


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


@pytest.mark.parametrize("how", ["close", "cancel"])
async def test_close_or_cancel_before_process_start_leaves_no_process(
    mcp_home, tmp_path, slow_spawn, mcp_reaper, mcp_owner, how
):
    control = tmp_path / "s.json"
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)

    _conn(consumer).start_connect(entry)
    task = _attempt(consumer, entry)
    await asyncio.sleep(0.15)  # поток уже в запуске, процесса ещё нет
    if how == "close":
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)
    else:
        task.cancel()
        await asyncio.wait({task}, timeout=_CEILING_SEC)

    await wait_for(lambda: len(slow_spawn) == 1)
    [proc] = slow_spawn
    await wait_for(lambda: proc.poll() is not None)
    assert _owner_records(consumer) == []
    assert consumer.status_of(entry).state == STATE_FAILED


async def test_interrupted_close_is_finished_by_second_close(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    deaf, stream = tmp_path / "deaf.json", tmp_path / "stream.json"
    set_behavior(deaf, ignore_term=True, ignore_eof=True)
    settings = _settings(
        {
            "deaf": stdio_cfg(deaf),
            "stream": {**stdio_cfg(stream), "transport": "stream_stdio"},
        }
    )
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entries = [_entry(service, settings, k) for k in ("deaf", "stream")]
    for entry in entries:
        await _connect(consumer, entry)
    pids = launches(deaf) + launches(stream)

    # Срок хоста истекает, пока глухой к SIGTERM сервер ждёт эскалации.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(consumer.aclose(), 0.3)
    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    await wait_for(lambda: not any(alive(p) for p in pids), 15)
    assert _owner_records(consumer) == []
    assert consumer not in service._consumers


async def test_series_stops_when_connected_during_pause(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    sleep = _ParkedSleep(clock)
    attempts = _count_attempts(monkeypatch)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    settings = _settings({"srv": {"transport": "stream_http", "url": url}})
    service = McpService(connect_on_start=True, clock=clock.now, sleep=sleep)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _in_set(consumer, _entry(service, settings))
    await _connect(consumer, entry)
    await wait_for(lambda: sleep.pauses == [1])

    async with stream_server(tmp_path, "stream_http", port=port):
        await _connect(consumer, entry)  # подключение мимо серии
        assert consumer.status_of(entry).state == STATE_CONNECTED
        clock.advance(1)
        await wait_for(lambda: not _conn(consumer)._series)
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert len(attempts) == 2


async def test_cancelled_waiter_keeps_attempt_and_series(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    sleep = _ParkedSleep(clock)
    gate = asyncio.Event()
    calls = []

    async def gated_list(server_key, server_cfg, *, timeout):
        calls.append(server_key)
        if len(calls) > 1:
            await gate.wait()
        raise ConnectionRefusedError("нет соединения")

    monkeypatch.setattr(connections_module, "alist_raw_tools", gated_list)
    settings = _settings({"srv": _closed_port_cfg()})
    service = McpService(connect_on_start=True, clock=clock.now, sleep=sleep)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _in_set(consumer, _entry(service, settings))
    await _connect(consumer, entry)
    await wait_for(lambda: sleep.pauses == [1])
    clock.advance(1)
    await wait_for(lambda: len(calls) == 2)

    # Внешний ожидающий той же попытки сдаётся по своему сроку.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_conn(consumer).start_connect(entry), 0.1)
    still_running = not _attempt(consumer, entry).done()
    series_alive = bool(_conn(consumer)._series)
    gate.set()
    await wait_for(lambda: sleep.pauses == [1, 3])

    assert (still_running, series_alive) == (True, True)
    status = consumer.status_of(entry)
    assert (status.state, status.reason_code) == (
        STATE_FAILED,
        "connect_error",
    )


async def test_failure_record_error_still_drops_session(
    mcp_home, tmp_path, monkeypatch, capsys, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)
    await _connect(consumer, entry)
    [pid] = launches(control)

    def broken_record(entry, exc):
        raise RuntimeError("классификация сломалась")

    monkeypatch.setattr(service, "record_failure", broken_record)
    set_behavior(control, exit_on="tools/list")
    await _connect(consumer, entry)

    status = consumer.status_of(entry)
    assert (status.state, status.reason_code, status.reason) == (
        STATE_FAILED,
        "other",
        "внутренняя ошибка подключения",
    )
    assert not _conn(consumer).has_session(entry)
    await wait_for(lambda: not alive(pid))
    assert capsys.readouterr().out == ""


async def test_start_connect_after_close_is_refused(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    settings = _settings({"srv": stdio_cfg(tmp_path / "s.json")})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)
    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    with pytest.raises(McpServiceClosed, match="сервис MCP остановлен"):
        _conn(consumer).start_connect(entry)
    assert launches(tmp_path / "s.json") == []


async def test_close_waits_for_closing_tasks(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)
    started, finished = asyncio.Event(), []
    real_close = _conn(consumer)._aclose_session

    async def slow_close(entry):
        started.set()
        await asyncio.sleep(0.3)
        await real_close(entry)
        finished.append(entry.key)

    _conn(consumer)._aclose_session = slow_close
    _conn(consumer).start_connect(entry)
    await asyncio.wait_for(started.wait(), _CEILING_SEC)

    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert finished == ["srv"]
    assert _conn(consumer)._closing == set()


async def test_cancel_before_first_step_keeps_live_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    settings = _settings({"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = _consumer(mcp_reaper, service, settings)
    entry = _entry(service, settings)
    await _connect(consumer, entry)
    [pid] = launches(control)

    _conn(consumer).start_connect(entry)
    task = _attempt(consumer, entry)
    task.cancel()
    await asyncio.wait({task}, timeout=_CEILING_SEC)

    assert consumer.status_of(entry).state == STATE_CONNECTED
    assert _conn(consumer).has_session(entry)
    with use_owner(consumer.owner):
        out = await call_tool(
            "srv", _conn(consumer).wrapper_cfg(entry), "echo", {"text": "hi"}
        )
    assert out["pid"] == pid
