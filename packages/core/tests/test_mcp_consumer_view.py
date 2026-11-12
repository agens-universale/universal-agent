"""Срез тулов потребителя сервиса MCP и снимок статусов (спека §5.2, §5.7).

Срез отдаёт сборке то же, что прежний путь обнаружения: записи реестра
тулов, статусы серверов, определения для обёрток и исполнителя вызова. Он
закреплён (фоновые изменения видны после ``pin()``) и строится только для
серверов из определений вызывающего с тем же отпечатком.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from gc_core.tools.connect_mcp import (
    consumer_connections as connections_module,
)
from gc_core.tools.connect_mcp.agent_tools import (
    _assemble_mcp_tools_data,
    mcp_cache_config,
)
from gc_core.tools.connect_mcp.consumer_status import (
    INTERRUPTED_REASON,
    STATE_CACHED,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_FAILED,
)
from gc_core.tools.connect_mcp.registry import (
    ToolsCache,
    mcp_tool_full_name,
)
from gc_core.tools.connect_mcp.server_identity import (
    SESSION_KEY_FIELD,
    session_key,
)
from gc_core.tools.connect_mcp.service import McpService
from gc_core.tools.mcp_snapshot import (
    build_mcp_tools_snapshot,
    overlay_service_statuses,
)
from gc_testkit.mcp import (
    FakeClock,
    IsolatedBackend,
    launches,
    seed_catalog,
    set_behavior,
    stdio_cfg,
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


def _conn(consumer):
    """Соединения потребителя — владелец его состояния."""
    return consumer._connections


def _settings(servers: dict, **extra):
    return make_settings(
        {
            "mcp": {
                "servers": servers,
                "meta": {"discovery_timeout_sec": 30},
            },
            **extra,
        }
    )


def _whitelisted(cfg: dict) -> dict:
    return {**cfg, "whitelist": {"enabled": True, "tools": ["echo"]}}


async def _ready(reaper, service, settings, label="c", **kwargs):
    """Потребитель, сверивший набор с ``settings``."""
    consumer = reaper.track(service.open_consumer(label))
    build = consumer.next_build()
    await asyncio.wait_for(
        consumer.aensure(build, settings, **kwargs), _CEILING_SEC
    )
    return consumer


async def _view(consumer, settings, **kwargs):
    return await asyncio.wait_for(
        consumer.aview(settings, **kwargs), _CEILING_SEC
    )


def _tools_of(view, key: str) -> list[str]:
    return sorted(
        name
        for name, entry in view.registry.items()
        if entry["metadata"]["mcp_server_key"] == key
    )


async def test_view_has_entries_statuses_and_keys(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)

    view = await _view(consumer, settings)

    entry = consumer.servers()["a"]
    assert _ECHO in view.registry
    assert view.status["a"]["status"] == STATE_CONNECTED
    assert view.servers_cfg["a"][SESSION_KEY_FIELD] == session_key(
        "a", entry.fingerprint
    )
    assert view.servers_cfg["a"]["persistent_session"] is True


async def test_cached_entry_offers_tools(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    seed_catalog(settings, "a", ["echo", "nap"])
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)

    view = await _view(consumer, settings)

    assert _tools_of(view, "a") == sorted([_ECHO, _NAP])
    assert view.status["a"]["status"] == STATE_CACHED
    assert view.status["a"]["from_catalog"] is True
    assert launches(control) == []


async def test_view_only_for_callers_servers(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    seed_catalog(settings, "a", ["echo"])
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)
    other = _settings({"b": stdio_cfg(tmp_path / "b.json")})

    view = await _view(consumer, other)

    assert (view.registry, view.status, view.servers_cfg) == ({}, {}, {})


async def test_view_skips_server_with_other_fingerprint(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    cfg = stdio_cfg(tmp_path / "a.json")
    settings = _settings({"a": cfg})
    seed_catalog(settings, "a", ["echo"])
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)
    changed = _settings({"a": {**cfg, "args": [*cfg["args"], "--x"]}})

    view = await _view(consumer, changed)

    assert (view.registry, view.status, view.servers_cfg) == ({}, {}, {})


async def test_view_applies_callers_whitelist(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    cfg = stdio_cfg(tmp_path / "a.json")
    settings = _settings({"a": cfg})
    seed_catalog(settings, "a", ["echo", "nap"])
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)

    view = await _view(consumer, _settings({"a": _whitelisted(cfg)}))

    assert _tools_of(view, "a") == [_ECHO]
    assert view.status["a"]["tool_count"] == 1


async def test_own_failure_hides_tools(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"a": stdio_cfg(control)})
    seed_catalog(settings, "a", ["echo"])
    service = McpService(connect_on_start=True, wait_all=True)
    consumer = await _ready(mcp_reaper, service, settings)

    view = await _view(consumer, settings)

    assert view.registry == {}
    assert view.status["a"]["status"] == STATE_FAILED
    assert view.status["a"]["tool_count"] == 0
    assert view.status["a"]["reason"]


async def test_other_consumers_failure_keeps_tools(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True, wait_all=True)
    first = await _ready(mcp_reaper, service, settings, "A")
    set_behavior(control, fail_start=True)
    second = await _ready(mcp_reaper, service, settings, "B")

    first.pin()
    view_a = await _view(first, settings)
    view_b = await _view(second, settings)

    assert _tools_of(view_a, "a") == sorted([_ECHO, _NAP])
    assert view_a.status["a"]["status"] == STATE_CONNECTED
    assert view_b.registry == {}
    assert view_b.status["a"]["status"] == STATE_FAILED


async def test_view_is_pinned_until_next_pin(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    first = await _view(consumer, settings)

    async def refused(server_key, server_cfg, *, timeout):
        raise ConnectionError("сервер не отвечает")

    monkeypatch.setattr(connections_module, "alist_raw_tools", refused)
    entry = consumer.servers()["a"]
    connected = await asyncio.wait_for(
        _conn(consumer).start_connect(entry), _CEILING_SEC
    )
    second = await _view(consumer, settings)
    changed = consumer.pin()
    third = await _view(consumer, settings)

    assert connected is False
    assert second == first
    assert changed is True
    assert third.registry == {}
    assert third.status["a"]["status"] == STATE_FAILED


async def test_trace_goes_to_wrapper_cfg_not_to_fingerprint(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    server = {**stdio_cfg(tmp_path / "a.json"), "trace_header_enabled": True}
    seed_catalog(_settings({"a": server}), "a", ["echo"])
    service = McpService(connect_on_start=False)
    keys = []
    for trace in ("t1", "t2"):
        settings = _settings({"a": server}, trace_id=trace)
        consumer = await _ready(mcp_reaper, service, settings, trace)
        view = await _view(consumer, settings)
        assert view.servers_cfg["a"]["env"]["TRACE_ID"] == trace
        keys.append(view.servers_cfg["a"][SESSION_KEY_FIELD])

    assert keys[0] == keys[1]


def _backend_settings(control) -> object:
    return _settings({"a": {**stdio_cfg(control), "execute_on": "os_backend"}})


async def test_backend_part_uses_build_backend_and_is_cached(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _backend_settings(control)
    first, second = IsolatedBackend(), IsolatedBackend()
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings, os_backend=first)

    views = [
        await _view(consumer, settings, os_backend=first),
        await _view(consumer, settings, os_backend=first),
    ]
    third = await _view(consumer, settings, os_backend=second)

    assert (len(first.methods), len(second.methods)) == (1, 1)
    assert all(_tools_of(v, "a") == [_ECHO] for v in [*views, third])
    assert third.status["a"]["status"] == STATE_CONNECTED
    assert (
        third.servers_cfg["a"]
        == mcp_cache_config(settings)["mcp-servers"]["a"]
    )
    assert launches(control) == []


async def test_service_backend_cache_serves_backend_part(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    agent_control = tmp_path / "b.json"
    agent_side = stdio_cfg(agent_control)
    settings = _settings(
        {
            "a": {**stdio_cfg(control), "execute_on": "os_backend"},
            "b": agent_side,
        }
    )
    seed_catalog(settings, "b", ["echo"])
    surface = IsolatedBackend()
    service = McpService(connect_on_start=False, os_backend=surface)
    first = await _ready(
        mcp_reaper, service, settings, "A", os_backend=surface
    )
    second = await _ready(
        mcp_reaper, service, settings, "B", os_backend=surface
    )

    view_a = await _view(first, settings, os_backend=surface)
    view_b = await _view(second, settings, os_backend=surface)

    assert len(surface.methods) == 1
    assert view_a.registry == view_b.registry
    assert set(view_a.status) == {"a", "b"}
    assert launches(control) == []
    # В кэш бэкенда ушёл только сервер бэкенда: агентный b не обнаруживался
    # прежним путём и остался со статусом сервиса.
    assert launches(agent_control) == []
    assert view_a.status["b"]["status"] == STATE_CACHED
    assert SESSION_KEY_FIELD in view_a.servers_cfg["b"]


async def test_status_snapshot_from_another_thread(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)

    snap = await asyncio.wait_for(
        asyncio.to_thread(service.status, consumer), _CEILING_SEC
    )

    assert set(snap) == {"a"}
    assert snap["a"].state == STATE_CONNECTED
    assert isinstance(snap["a"].tools, tuple)
    assert [t["name"] for t in snap["a"].tools] == ["echo", "nap"]


def _build_snapshot(view, settings):
    tools_data, status = _assemble_mcp_tools_data(
        view.registry, view.status, settings, view.servers_cfg
    )
    return build_mcp_tools_snapshot(settings, tools_data, status), status


async def test_overlay_matches_build_snapshot(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    cfg = _whitelisted(stdio_cfg(tmp_path / "a.json"))
    own = _settings({"a": cfg})
    seed_catalog(own, "a", ["echo", "nap"])
    both = _settings({"a": cfg, "b": stdio_cfg(tmp_path / "b.json")})
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, own)
    snap0, status0 = _build_snapshot(await _view(consumer, both), both)
    entry = consumer.servers()["a"]
    assert await asyncio.wait_for(
        _conn(consumer).start_connect(entry), _CEILING_SEC
    )

    snap, status = overlay_service_statuses(
        snap0, status0, both, service.status(consumer)
    )

    consumer.pin()
    expected, _ = _build_snapshot(await _view(consumer, both), both)
    assert snap0["a"]["status"] == STATE_CACHED
    assert snap["a"] == expected["a"]
    assert snap["a"]["status"] == STATE_CONNECTED
    assert [t["name"] for t in snap["a"]["tools"]] == [_ECHO]
    assert status["a"]["status"] == STATE_CONNECTED
    assert snap["b"] == snap0["b"]
    assert "b" not in status


async def test_overlay_keeps_connecting_and_empties_failed(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """``connecting`` наложение не трогает; у ``failed`` тулов нет, причина
    и код — из статуса сервиса."""
    failing, slow = tmp_path / "a.json", tmp_path / "c.json"
    set_behavior(failing, fail_start=True)
    set_behavior(slow, start_delay=10)
    settings = _settings({"a": stdio_cfg(failing), "c": stdio_cfg(slow)})
    seed_catalog(settings, "c", ["echo"])
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    statuses = service.status(consumer)
    assert statuses["c"].state == "connecting"
    assert statuses["a"].state == STATE_FAILED
    # Снимок сборки с прежними значениями: у a — тулы, у c — свои отметки.
    stale_tool = {"name": _ECHO, "description": "", "tool": "echo"}
    snap0 = {
        "a": {
            "status": STATE_CONNECTED,
            "reason": None,
            "description": "",
            "tools": [stale_tool],
        },
        "c": {
            "status": "unknown",
            "reason": None,
            "description": "",
            "tools": [],
        },
    }
    status0 = {
        "a": {"status": STATE_CONNECTED, "reason": None, "tool_count": 1},
        "c": {"status": "unknown", "reason": None, "tool_count": 0},
    }

    snap, status = overlay_service_statuses(snap0, status0, settings, statuses)

    assert snap["c"] is snap0["c"]
    assert status["c"] is status0["c"]
    assert snap["a"]["status"] == STATE_FAILED
    assert snap["a"]["tools"] == []
    assert snap["a"]["reason"] == statuses["a"].reason
    assert status["a"]["reason_code"] == statuses["a"].reason_code
    assert status["a"]["tool_count"] == 0
    assert snap0["a"]["tools"] == [stale_tool]


async def test_closed_consumer_view_is_pinned(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    before = await _view(consumer, settings)

    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert await _view(consumer, settings) == before


async def test_closed_consumer_status_is_not_connecting(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Закрытый потребитель не подключается: сервер без тулов и без
    попыток показывает свежий отказ записи, а после 30 с — «подключение
    прервано», а не вечное ``connecting``."""
    control = tmp_path / "a.json"
    set_behavior(control, fail_start=True)
    settings = _settings({"a": stdio_cfg(control)})
    clock = FakeClock()
    service = McpService(connect_on_start=True, clock=clock.now)
    failing = await _ready(mcp_reaper, service, settings, "A")
    idle = await _ready(mcp_reaper, service, settings, "B")
    assert len(launches(control)) == 1

    await asyncio.wait_for(idle.aclose(), _CEILING_SEC)
    fresh = service.status(idle)["a"]
    clock.advance(30)
    later = service.status(idle)["a"]

    shared = idle.servers()["a"].last_failure
    assert (fresh.state, fresh.reason_code, fresh.reason) == (
        STATE_FAILED,
        shared.code,
        shared.reason,
    )
    assert fresh.reason != INTERRUPTED_REASON
    assert (later.state, later.reason_code, later.reason) == (
        STATE_FAILED,
        "other",
        INTERRUPTED_REASON,
    )
    assert service.status(failing)["a"].state == STATE_FAILED


async def test_view_matches_legacy_discovery(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Страж совместимости: прежнее обнаружение и срез потребителя дают
    одинаковые записи реестра и статус одного сервера."""
    settings = _settings({"a": _whitelisted(stdio_cfg(tmp_path / "a.json"))})
    legacy = ToolsCache(mcp_cache_config(settings))
    registry = await asyncio.wait_for(legacy.aget_tools(), _CEILING_SEC)
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)

    view = await _view(consumer, settings)

    old = legacy.get_status()["a"]
    assert view.registry == registry
    assert {
        k: view.status["a"][k] for k in ("status", "reason", "tool_count")
    } == {k: old[k] for k in ("status", "reason", "tool_count")}


async def test_connecting_entry_offers_catalog_tools(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Сервер в ``connecting`` с тулами записи предлагает их модели."""
    control = tmp_path / "a.json"
    set_behavior(control, start_delay=1.0)
    settings = _settings({"a": stdio_cfg(control)})
    seed_catalog(settings, "a", ["echo"])
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)

    view = await _view(consumer, settings)

    assert view.status["a"]["status"] == STATE_CONNECTING
    assert view.status["a"]["from_catalog"] is True
    assert _tools_of(view, "a") == [_ECHO]


_BAD_TOOLS = [{"name": "echo", "description": {"text": "x"}}, {"name": "nap"}]


def _write_raw_catalog(settings, server_key: str, tools: list) -> None:
    """Каталог с сырым списком как есть (записан до проверки списка)."""
    from gc_core.config_modules.field_meta import plain_dump
    from gc_core.tools.connect_mcp.catalog import write_catalog
    from gc_core.tools.connect_mcp.effective import effective_mcp_servers

    model = effective_mcp_servers(settings)[server_key]
    write_catalog(server_key, plain_dump(model, by_alias=True), tools)


async def test_bad_catalog_list_fails_only_that_server(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Каталог с негодным описанием тула не роняет срез: этот сервер —
    отказ ``protocol`` без тулов, соседний отдаёт свои тулы и в сборке."""
    from gc_core.tools.connect_mcp.agent_tools import (
        aget_mcp_tools_data_for_config,
    )

    settings = _settings(
        {
            "a": stdio_cfg(tmp_path / "a.json"),
            "b": stdio_cfg(tmp_path / "b.json"),
        }
    )
    seed_catalog(settings, "a", ["echo"])
    _write_raw_catalog(settings, "b", _BAD_TOOLS)
    service = McpService(connect_on_start=False)
    consumer = await _ready(mcp_reaper, service, settings)

    view = await _view(consumer, settings)
    tools_data, status = await asyncio.wait_for(
        aget_mcp_tools_data_for_config(settings, tools_cache=consumer),
        _CEILING_SEC,
    )

    assert _tools_of(view, "a") == [_ECHO]
    assert _tools_of(view, "b") == []
    assert view.status["b"]["status"] == STATE_FAILED
    assert view.status["b"]["reason_code"] == "protocol"
    assert "«echo»" in view.status["b"]["reason"]
    assert view.status["a"]["status"] == STATE_CACHED
    assert _ECHO in tools_data
    assert status["b"]["status"] == STATE_FAILED


def test_overlay_isolates_bad_list(tmp_path):
    """Наложение статусов (снимок окна) с негодным списком сервера не
    бросает: этот сервер — отказ ``protocol``, соседний — с тулами."""
    from gc_core.tools.connect_mcp.consumer_status import ServerStatus

    settings = _settings(
        {
            "a": stdio_cfg(tmp_path / "a.json"),
            "b": stdio_cfg(tmp_path / "b.json"),
        }
    )
    statuses = {
        "a": ServerStatus(STATE_CONNECTED, tools=({"name": "echo"},)),
        "b": ServerStatus(STATE_CONNECTED, tools=tuple(_BAD_TOOLS)),
    }

    snapshot, status = overlay_service_statuses({}, {}, settings, statuses)

    assert [t["name"] for t in snapshot["a"]["tools"]] == [_ECHO]
    assert snapshot["b"]["tools"] == []
    assert snapshot["b"]["status"] == STATE_FAILED
    assert status["b"]["reason_code"] == "protocol"
    assert status["a"]["status"] == STATE_CONNECTED


async def test_bad_catalog_warns_once_per_record(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, caplog
):
    """Негодный каталог — одно предупреждение на запись сервера, а не на
    каждый срез (сборка, вход в ход, наложение статусов)."""
    import logging

    settings = _settings({"b": stdio_cfg(tmp_path / "b.json")})
    _write_raw_catalog(settings, "b", _BAD_TOOLS)
    service = McpService(connect_on_start=False)
    with caplog.at_level(logging.DEBUG, logger="gc_core.tools.connect_mcp"):
        consumer = await _ready(mcp_reaper, service, settings)
        for _ in range(3):
            await _view(consumer, settings)
        overlay_service_statuses({}, {}, settings, service.status(consumer))

    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "«echo»" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_bad_list_reason_masked_and_truncated(tmp_path):
    """Причина отказа по негодному списку — с той же маской секретов и
    обрезкой, что на пути подключения."""
    from gc_core.tools.connect_mcp.consumer_status import ServerStatus
    from gc_core.tools.connect_mcp.consumer_view import server_offer

    secret = "supersecretvalue1"
    cfg = {**stdio_cfg(tmp_path / "b.json"), "env": {"API_KEY": secret}}
    servers = mcp_cache_config(_settings({"b": cfg}))["mcp-servers"]
    bad_name = secret + "x" * 200
    status = ServerStatus(
        STATE_CONNECTED,
        tools=({"name": bad_name, "description": {"text": "x"}},),
    )

    _, failed = server_offer("b", servers["b"], status)

    assert failed.state == STATE_FAILED
    assert failed.reason_code == "protocol"
    assert secret not in failed.reason
    assert failed.reason.endswith("…")
    assert len(failed.reason) <= 121
