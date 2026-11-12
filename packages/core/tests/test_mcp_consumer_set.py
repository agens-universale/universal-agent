"""Потребитель сервиса MCP: набор серверов сборки и сверка разницей.

Набор задаёт последняя начатая сборка. Новые серверы подключаются по
политике хоста, убранные и изменённые закрываются в фоне, неизменные не
трогаются. Сборка ждёт подключения только там, где тулы ещё неизвестны и
отказа ещё не было; ``wait_all`` (``gc-cli``) ждёт все свои подключения.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest
from gc_core.mcp_session_owner import use_owner
from gc_core.tools.connect_mcp import consumer as consumer_module
from gc_core.tools.connect_mcp.client import _MCP_SESSIONS
from gc_core.tools.connect_mcp.consumer_status import (
    STATE_CACHED,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_FAILED,
)
from gc_core.tools.connect_mcp.server_identity import session_key
from gc_core.tools.connect_mcp.service import McpService
from gc_core.tools.connect_mcp.service_records import McpServiceClosed
from gc_testkit.mcp import (
    FakeClock,
    alive,
    call_tool,
    events,
    exits,
    launches,
    seed_catalog,
    set_behavior,
    stdio_cfg,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.settings import make_settings

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0


def _settings(servers: dict, *, discovery: int = 30):
    return make_settings(
        {
            "mcp": {
                "servers": servers,
                "meta": {"discovery_timeout_sec": discovery},
            }
        }
    )


def _open(reaper, service: McpService, label: str = "c"):
    return reaper.track(service.open_consumer(label))


async def _ensure(consumer, settings, **kwargs) -> None:
    build = consumer.next_build()
    await asyncio.wait_for(
        consumer.aensure(build, settings, **kwargs), _CEILING_SEC
    )


async def _settle(consumer) -> None:
    """Дождаться фонового закрытия убранных сессий."""
    await asyncio.wait_for(
        asyncio.gather(*list(consumer._closing)), _CEILING_SEC
    )


def _state(consumer, key: str) -> str:
    return consumer.status_of(consumer.servers()[key]).state


def _with_args(cfg: dict, *extra: str) -> dict:
    return {**cfg, "args": [*cfg["args"], *extra]}


def _gate_close(consumer, key: str):
    """Закрытие сессии сервера ``key`` ждёт, пока тест откроет ворота."""
    gate, entered = asyncio.Event(), asyncio.Event()
    real = consumer._aclose_session

    async def gated(entry):
        if entry.key == key:
            entered.set()
            await gate.wait()
        await real(entry)

    consumer._aclose_session = gated
    return gate, entered


async def test_changing_one_of_three_touches_only_it(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    controls = {key: tmp_path / f"{key}.json" for key in "abc"}
    servers = {key: stdio_cfg(control) for key, control in controls.items()}
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))
    await _ensure(consumer, _settings(servers))
    pids = {key: launches(control)[0] for key, control in controls.items()}
    old_b = consumer.servers()["b"]

    changed = {**servers, "b": _with_args(servers["b"], "--changed")}
    await _ensure(consumer, _settings(changed))
    await _settle(consumer)

    assert alive(pids["a"]) and alive(pids["c"])
    assert launches(controls["a"]) == [pids["a"]]
    assert launches(controls["c"]) == [pids["c"]]
    assert pids["b"] in exits(controls["b"])
    assert consumer.servers()["b"] is not old_b
    old_pid, new_pid = launches(controls["b"])
    assert old_pid == pids["b"]
    assert alive(new_pid)
    assert _state(consumer, "b") == STATE_CONNECTED


async def test_whitelist_edit_keeps_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    cfg = stdio_cfg(control)
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))
    await _ensure(consumer, _settings({"a": cfg}))
    entry = consumer.servers()["a"]
    [pid] = launches(control)

    narrowed = {**cfg, "whitelist": {"enabled": True, "tools": ["echo"]}}
    await _ensure(consumer, _settings({"a": narrowed}))
    await _settle(consumer)

    assert consumer.servers()["a"] is entry
    assert alive(pid)
    assert launches(control) == [pid]
    assert _state(consumer, "a") == STATE_CONNECTED


async def test_removed_server_session_is_closed(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))
    await _ensure(consumer, _settings({"a": stdio_cfg(control)}))
    [pid] = launches(control)

    await _ensure(consumer, _settings({}))
    await _settle(consumer)

    assert pid in exits(control)
    assert "a" not in consumer.servers()


async def test_stale_build_does_not_change_set(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    servers = {key: stdio_cfg(tmp_path / f"{key}.json") for key in "ab"}
    for key in servers:
        seed_catalog(_settings(servers), key, ["echo"])
    consumer = _open(mcp_reaper, McpService(connect_on_start=False))
    old, new = consumer.next_build(), consumer.next_build()

    await asyncio.wait_for(
        consumer.aensure(new, _settings({"a": servers["a"]})), _CEILING_SEC
    )
    await asyncio.wait_for(
        consumer.aensure(old, _settings({"b": servers["b"]})), _CEILING_SEC
    )

    assert set(consumer.servers()) == {"a"}
    assert launches(tmp_path / "b.json") == []


@pytest.mark.parametrize("cataloged", [True, False])
async def test_on_start_waits_only_without_catalog(
    cataloged, mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=1.5)
    settings = _settings({"a": stdio_cfg(control)})
    if cataloged:
        seed_catalog(settings, "a", ["echo", "nap"])
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))

    await _ensure(consumer, settings)

    status = consumer.status_of(consumer.servers()["a"])
    if cataloged:
        assert status.state == STATE_CONNECTING
        assert [tool["name"] for tool in status.tools] == ["echo", "nap"]
    else:
        assert status.state == STATE_CONNECTED


async def test_wait_all_waits_even_with_catalog(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=1.5)
    settings = _settings({"a": stdio_cfg(control)})
    seed_catalog(settings, "a", ["echo", "nap"])
    service = McpService(connect_on_start=True, wait_all=True)
    consumer = _open(mcp_reaper, service)

    await _ensure(consumer, settings)

    assert _state(consumer, "a") == STATE_CONNECTED


async def test_lazy_with_catalog_launches_nothing(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    seed_catalog(settings, "a", ["echo"])
    consumer = _open(mcp_reaper, McpService(connect_on_start=False))

    await _ensure(consumer, settings)

    assert _state(consumer, "a") == STATE_CACHED
    assert launches(control) == []


async def test_lazy_without_catalog_connects_at_build(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    consumer = _open(mcp_reaper, McpService(connect_on_start=False))

    await _ensure(consumer, _settings({"a": stdio_cfg(control)}))

    assert _state(consumer, "a") == STATE_CONNECTED
    assert len(launches(control)) == 1


async def test_rebuild_does_not_hammer_failed_server(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"a": stdio_cfg(control)})
    clock = FakeClock()
    service = McpService(
        connect_on_start=True, clock=clock.now, sleep=clock.sleep
    )
    consumer = _open(mcp_reaper, service)

    await _ensure(consumer, settings)
    first = len(launches(control))
    await _ensure(consumer, settings)
    rebuilt = len(launches(control))
    attempting = bool(consumer._connects)
    clock.advance(30)
    await _ensure(consumer, settings)
    await wait_for(lambda: len(launches(control)) == 2, _CEILING_SEC)
    await wait_for(lambda: _state(consumer, "a") == STATE_FAILED)

    assert (first, rebuilt) == (1, 1)
    assert not attempting
    assert len(launches(control)) == 2


async def test_build_does_not_wait_after_failure(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"a": stdio_cfg(control)})
    clock = FakeClock()
    service = McpService(
        connect_on_start=True, clock=clock.now, sleep=clock.sleep
    )
    consumer = _open(mcp_reaper, service)
    await _ensure(consumer, settings)
    assert _state(consumer, "a") == STATE_FAILED

    set_behavior(control, start_delay=5, fail_start=False)
    clock.advance(30)
    await asyncio.wait_for(
        consumer.aensure(consumer.next_build(), settings), 1
    )

    assert _state(consumer, "a") == STATE_CONNECTING


async def test_build_wait_is_bounded(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=5)
    settings = _settings({"a": stdio_cfg(control)}, discovery=1)
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))

    await asyncio.wait_for(
        consumer.aensure(consumer.next_build(), settings), 4
    )
    entry = consumer.servers()["a"]
    await wait_for(lambda: consumer.status_of(entry).state == STATE_FAILED)
    await wait_for(
        lambda: any(line.startswith("exit ") for line in events(control))
    )

    assert consumer.status_of(entry).reason_code == "timeout"


async def test_cancelled_ensure_keeps_new_set(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    first = stdio_cfg(tmp_path / "a.json")
    seed_catalog(_settings({"a": first}), "a", ["echo"])
    set_behavior(tmp_path / "b.json", start_delay=5)
    consumer = _open(mcp_reaper, McpService(connect_on_start=False))
    await _ensure(consumer, _settings({"a": first}))

    building = asyncio.create_task(
        consumer.aensure(
            consumer.next_build(),
            _settings({"b": stdio_cfg(tmp_path / "b.json")}),
        )
    )
    await wait_for(lambda: bool(consumer._connects))
    building.cancel()
    await asyncio.wait({building}, timeout=_CEILING_SEC)

    assert building.cancelled()
    assert set(consumer.servers()) == {"b"}
    assert _state(consumer, "b") == STATE_CONNECTING


class _Sandbox:
    """Изолированный бэкенд: команды исполняются не в нашем процессе."""

    is_sandbox = True


async def test_backend_side_servers_are_not_in_set(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    cfg = {**stdio_cfg(control), "execute_on": "os_backend"}
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))

    await _ensure(consumer, _settings({"a": cfg}), os_backend=_Sandbox())

    assert "a" not in consumer.servers()
    assert launches(control) == []


async def test_two_on_start_consumers_have_own_sessions(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    first = _open(mcp_reaper, service, "first")
    second = _open(mcp_reaper, service, "second")

    await _ensure(first, settings)
    await _ensure(second, settings)
    await wait_for(lambda: _state(second, "a") == STATE_CONNECTED)

    assert first.servers()["a"] is second.servers()["a"]
    assert _state(first, "a") == STATE_CONNECTED
    pids = launches(control)
    assert len(set(pids)) == 2
    assert all(alive(pid) for pid in pids)


async def test_hundred_lazy_consumers_with_catalog(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    seed_catalog(settings, "a", ["echo"])
    service = McpService(connect_on_start=False)

    for number in range(100):
        consumer = service.open_consumer(f"c{number}")
        await _ensure(consumer, settings)
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert launches(control) == []
    assert service._consumers == set()


async def test_closed_consumer_rejects_ensure(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))
    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    with pytest.raises(McpServiceClosed, match="сервис MCP остановлен"):
        await _ensure(consumer, _settings({"a": stdio_cfg(control)}))
    assert launches(control) == []


async def test_stopped_service_rejects_ensure(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    service = McpService(connect_on_start=True)
    consumer = _open(mcp_reaper, service)
    service._stopped = True

    with pytest.raises(McpServiceClosed, match="сервис MCP остановлен"):
        await _ensure(consumer, _settings({"a": stdio_cfg(control)}))
    assert launches(control) == []


async def test_pin_reports_slice_change(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    seed_catalog(settings, "a", ["echo"])
    consumer = _open(mcp_reaper, McpService(connect_on_start=False))
    await _ensure(consumer, settings)
    entry = consumer.servers()["a"]

    unchanged = consumer.pin()
    await asyncio.wait_for(consumer.start_connect(entry), _CEILING_SEC)
    connected = consumer.pin()
    again = consumer.pin()

    assert (unchanged, connected, again) == (False, True, False)
    fingerprint, status = consumer._pinned["a"]
    assert fingerprint == entry.fingerprint
    assert status.state == STATE_CONNECTED


async def test_readded_server_waits_for_old_session_close(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))
    await _ensure(consumer, settings)
    entry = consumer.servers()["a"]
    [old_pid] = launches(control)
    real_close = consumer._aclose_session

    async def slow_close(entry):
        await asyncio.sleep(0.3)
        await real_close(entry)

    consumer._aclose_session = slow_close
    await _ensure(consumer, _settings({}))
    await _ensure(consumer, settings)
    await wait_for(lambda: len(launches(control)) == 2, _CEILING_SEC)
    await _settle(consumer)
    await wait_for(lambda: consumer.status_of(entry).state == STATE_CONNECTED)

    assert consumer.servers()["a"] is entry
    new_pid = launches(control)[1]
    journal = events(control)
    assert journal.index(f"exit {old_pid}") < journal.index(f"start {new_pid}")
    assert alive(new_pid)
    assert consumer.has_session(entry)


async def test_series_ends_when_server_left_set(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    """Серию заводит колбэк отказа попытки; если сверка убрала сервер
    раньше колбэка, серия кончается без попытки."""
    attempts: list[str] = []

    async def counting(server_key, server_cfg, *, timeout):
        attempts.append(server_key)
        raise ConnectionError("нет связи")

    monkeypatch.setattr(consumer_module, "alist_raw_tools", counting)
    clock = FakeClock()
    service = McpService(
        connect_on_start=False, clock=clock.now, sleep=clock.sleep
    )
    consumer = _open(mcp_reaper, service)
    old, new = (
        _settings({"a": {"transport": "stream_http", "url": url}})
        for url in ("http://old", "http://new")
    )
    for settings in (old, new):
        seed_catalog(settings, "a", ["echo"])
    await _ensure(consumer, old)
    left = consumer.servers()["a"]
    await _ensure(consumer, new)
    await _settle(consumer)

    consumer._start_series(left)
    await wait_for(lambda: not consumer._series)

    assert attempts == []
    assert consumer.servers()["a"] is not left


async def test_attempt_cut_by_removal_does_not_hold_readd(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=2)
    settings = _settings({"a": stdio_cfg(control)})
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))
    building = asyncio.create_task(
        consumer.aensure(consumer.next_build(), settings)
    )
    await wait_for(lambda: bool(consumer._connects))

    await _ensure(consumer, _settings({}))
    await asyncio.wait_for(building, _CEILING_SEC)
    await _settle(consumer)
    set_behavior(control)
    await _ensure(consumer, settings)

    assert _state(consumer, "a") == STATE_CONNECTED


async def test_call_in_flight_on_removed_server_ends_with_error(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Сверка закрывает сессию убранного сервера: начатый по ней вызов
    кончается ошибкой, а не ждёт своего срока."""
    control = tmp_path / "a.json"
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))
    await _ensure(consumer, _settings({"a": stdio_cfg(control)}))
    cfg = consumer.wrapper_cfg(consumer.servers()["a"])
    [pid] = launches(control)

    async def nap() -> dict:
        with use_owner(consumer.owner):
            return await call_tool(
                "a", cfg, "nap", {"seconds": 60}, timeout=60, ceiling=15
            )

    calling = asyncio.create_task(nap())
    await wait_for(lambda: any("tools/call" in e for e in events(control)))
    await _ensure(consumer, _settings({}))
    await asyncio.wait({calling}, timeout=_CEILING_SEC)

    assert calling.done()
    error = calling.exception()
    assert error is not None
    assert not isinstance(error, AssertionError)
    await _settle(consumer)
    assert pid in exits(control)


async def test_overtaken_build_does_not_pin(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Обогнанная сборка, проснувшись, не съедает признак «срез
    изменился» для следующего входа в ход."""
    slow = stdio_cfg(tmp_path / "a.json")
    set_behavior(tmp_path / "a.json", start_delay=5)
    cataloged = stdio_cfg(tmp_path / "b.json")
    seed_catalog(_settings({"b": cataloged}), "b", ["echo", "nap"])
    consumer = _open(mcp_reaper, McpService(connect_on_start=False))
    gate, entered = _gate_close(consumer, "a")
    old = asyncio.create_task(
        consumer.aensure(consumer.next_build(), _settings({"a": slow}))
    )
    await wait_for(lambda: bool(consumer._connects))

    await _ensure(consumer, _settings({"b": cataloged}))
    await asyncio.wait_for(entered.wait(), _CEILING_SEC)
    b = consumer.servers()["b"]
    await asyncio.wait_for(consumer.start_connect(b), _CEILING_SEC)
    assert _state(consumer, "b") == STATE_CONNECTED
    gate.set()
    await asyncio.wait_for(old, _CEILING_SEC)

    assert consumer.pin() is True


async def test_rest_close_is_tracked_by_key(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Закрытие остатка упавшей попытки числится за ключом сессии: сервер,
    убранный и сразу возвращённый, ждёт его до новой сессии."""
    control = tmp_path / "a.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"a": stdio_cfg(control)})
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))
    gate, entered = _gate_close(consumer, "a")
    building = asyncio.create_task(
        consumer.aensure(consumer.next_build(), settings)
    )
    await asyncio.wait_for(entered.wait(), _CEILING_SEC)
    entry = consumer.servers()["a"]
    key = session_key(entry.key, entry.fingerprint)
    assert consumer._closing_keys.get(key)

    await _ensure(consumer, _settings({}))
    await asyncio.wait_for(building, _CEILING_SEC)
    set_behavior(control)
    # Отказ попытки не записан (её сняли посреди закрытия остатка):
    # возвращённый сервер без тулов сборка ждёт, а попытка ждёт закрытие.
    readding = asyncio.create_task(
        consumer.aensure(consumer.next_build(), settings)
    )
    await wait_for(lambda: bool(consumer._connects))
    gate.set()
    await asyncio.wait_for(readding, _CEILING_SEC)
    await _settle(consumer)
    await wait_for(lambda: consumer.status_of(entry).state == STATE_CONNECTED)

    _, new_pid = launches(control)
    assert alive(new_pid)
    assert consumer.has_session(entry)


async def test_swallowed_cancel_does_not_leave_session_key(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Попытка, до которой отмена не дошла, успела записать сессию: после
    ухода сервера ключа нет, и возвращённый сервер подключается заново."""
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    consumer = _open(mcp_reaper, McpService(connect_on_start=False))
    real = consumer._alist_and_record

    async def swallowing(entry):
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        with consumer._lock:
            consumer._sessions.add(session_key(entry.key, entry.fingerprint))

    consumer._alist_and_record = swallowing
    building = asyncio.create_task(
        consumer.aensure(consumer.next_build(), settings)
    )
    await wait_for(lambda: bool(consumer._connects))
    await _ensure(consumer, _settings({}))
    await asyncio.wait_for(building, _CEILING_SEC)
    await _settle(consumer)

    assert consumer._sessions == set()
    consumer._alist_and_record = real
    await _ensure(consumer, settings)
    assert _state(consumer, "a") == STATE_CONNECTED
    assert len(launches(control)) == 1


async def test_close_during_build_wait_is_refused(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=5)
    consumer = _open(mcp_reaper, McpService(connect_on_start=True))
    building = asyncio.create_task(
        consumer.aensure(
            consumer.next_build(), _settings({"a": stdio_cfg(control)})
        )
    )
    await wait_for(lambda: bool(consumer._connects))

    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    with pytest.raises(McpServiceClosed, match="сервис MCP остановлен"):
        await asyncio.wait_for(building, _CEILING_SEC)


async def test_old_close_keeps_legacy_session_of_changed_server(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Идентификатор legacy-http ключуется именем: закрытие старой записи
    не снимает его у изменённого сервера с тем же ключом."""
    old = stdio_cfg(tmp_path / "a.json")
    new = _with_args(old, "--changed")
    for cfg in (old, new):
        seed_catalog(_settings({"a": cfg}), "a", ["echo"])
    consumer = _open(mcp_reaper, McpService(connect_on_start=False))
    await _ensure(consumer, _settings({"a": old}))
    _MCP_SESSIONS[(consumer.owner, "a")] = "sid-new"

    await _ensure(consumer, _settings({"a": new}))
    await _settle(consumer)

    assert _MCP_SESSIONS.get((consumer.owner, "a")) == "sid-new"
