"""Процессы серверов сервиса MCP запускаются через пускатель (спека §5.12).

Процесс сервера с постоянной сессией (постоянный stdio, ``stream_stdio``)
на POSIX запускается пускателем журнала сервиса: в журнале по записи на
запуск, запись снимается после закрытия сессии. Разовый stdio и серверы
бэкенда не журналируются. Причина отказа запуска («команда не найдена»)
остаётся понятной, отпечаток и каталог тулов пускатель не меняют.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess

import pytest
from gc_core.tools.connect_mcp.call_errors import McpServerGone
from gc_core.tools.connect_mcp.consumer_status import STATE_FAILED
from gc_core.tools.connect_mcp.process_journal import journal_root
from gc_core.tools.connect_mcp.registry import mcp_tool_full_name
from gc_core.tools.connect_mcp.service import McpService
from gc_core.tools.connect_mcp.stream_client import McpToolCallError
from gc_core.tools.connect_mcp.tool_wrapper import MCPToolWrapper
from gc_testkit.mcp import (
    BACKEND_ECHO_TEXT,
    FakeClock,
    IsolatedBackend,
    alive,
    connections_of,
    journal_records,
    launches,
    stdio_cfg,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.settings import make_settings

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="пускатель и журнал — только POSIX"
)

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
_ECHO = mcp_tool_full_name("a", "echo")
_NAP = mcp_tool_full_name("a", "nap")
_PROCESS_TRANSPORTS = ("stdio", "stream_stdio")


def _settings(servers: dict):
    return make_settings(
        {
            "mcp": {
                "servers": servers,
                "meta": {"discovery_timeout_sec": 20},
            }
        }
    )


def _server(control, transport: str = "stdio", **extra) -> dict:
    return {**stdio_cfg(control), "transport": transport, **extra}


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


async def _call(consumer, settings, name: str, *, os_backend=None, **args):
    """Вызов тула через срез потребителя — обёрткой, как у сборки."""
    view = await asyncio.wait_for(
        consumer.aview(settings, os_backend=os_backend), _CEILING_SEC
    )
    wrapper = MCPToolWrapper(
        name,
        view.servers_cfg,
        view.registry,
        20.0,
        default_use_retry=False,
        os_backend=os_backend,
        caller=view.caller,
    )
    return await asyncio.wait_for(wrapper.acall(**args), _CEILING_SEC)


def _payload(envelope: str) -> dict:
    """Ответ тестового сервера из конверта результата вызова."""
    result = json.loads(envelope)["result"]
    if isinstance(result, str):
        return json.loads(result)
    return json.loads(result["content"][0]["text"])


def _pid_of(envelope: str) -> int:
    return _payload(envelope)["pid"]


def _entry(consumer, key: str = "a"):
    return consumer.servers()[key]


def _launcher_args(consumer, key: str = "a") -> list[str]:
    """Аргументы пускателя в определении для обёрток: после ``--`` —
    команда сервера и её аргументы."""
    cfg = connections_of(consumer).wrapper_cfg(_entry(consumer, key))
    return cfg["args"]


def _journal_dirs() -> list:
    root = journal_root()
    return list(root.iterdir()) if root.exists() else []


@pytest.mark.parametrize("transport", _PROCESS_TRANSPORTS)
async def test_session_is_journaled(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, transport
):
    control = tmp_path / "a.json"
    settings = _settings({"a": _server(control, transport)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)

    pid = _pid_of(await _call(consumer, settings, _NAP, seconds=0.1))

    [record] = journal_records(service)
    assert record.pid == pid
    assert record.pgid == pid
    assert record.server == "a"
    assert record.error is None
    assert launches(control) == [pid]


async def test_restart_is_journaled(mcp_home, tmp_path, mcp_reaper, mcp_owner):
    clock = FakeClock()
    control = tmp_path / "a.json"
    settings = _settings({"a": _server(control)})
    service = McpService(connect_on_start=True, clock=clock.now)
    consumer = await _ready(mcp_reaper, service, settings)
    first = _pid_of(await _call(consumer, settings, _ECHO, text="hi"))

    os.kill(first, signal.SIGKILL)
    await wait_for(lambda: not alive(first), _CEILING_SEC)
    # Смерть замечает либо сама сессия (перезапуск в вызове), либо вызов
    # (отказ, а через 30 с — новое подключение); оба запуска — пускателем.
    try:
        out = await _call(consumer, settings, _ECHO, text="hi")
    except McpToolCallError:
        clock.advance(31)
        out = await _call(consumer, settings, _ECHO, text="hi")
    second = _pid_of(out)

    assert second != first
    assert second in [r.pid for r in journal_records(service)]
    await asyncio.wait_for(service.aprune_journal(), _CEILING_SEC)
    assert [r.pid for r in journal_records(service)] == [second]


@pytest.mark.parametrize("transport", _PROCESS_TRANSPORTS)
async def test_record_removed_after_session_close(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, transport
):
    control = tmp_path / "a.json"
    settings = _settings({"a": _server(control, transport)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    [pid] = launches(control)
    assert [r.pid for r in journal_records(service)] == [pid]

    # Закрытие по ключу без сверки: запись снимает именно чистка после
    # закрытия сессии, а не чистка сверки.
    connections = connections_of(consumer)
    await asyncio.wait_for(connections.adrop(_entry(consumer)), _CEILING_SEC)

    assert journal_records(service) == []
    assert not alive(pid)


async def test_record_removed_after_consumer_close(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": _server(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    assert len(journal_records(service)) == 1

    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert journal_records(service) == []


async def test_reconcile_prunes_exited_processes(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    # Запись процесса, который вышел и пожат, без закрытия сессии по
    # ключу (перезапуск внутри сессии) снимает следующая сверка; запись
    # живого процесса остаётся.
    control = tmp_path / "a.json"
    settings = _settings({"a": _server(control)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    [live] = journal_records(service)
    exited = subprocess.Popen(["true"])  # noqa: S607
    exited.wait()
    pid = exited.pid
    record = {"pid": pid, "pgid": pid, "mark": "1", "server": "a"}
    (live.path.parent / f"{pid}.json").write_text(json.dumps(record))
    assert len(journal_records(service)) == 2

    await _ensure(consumer, settings)

    assert journal_records(service) == [live]


async def test_one_shot_stdio_is_not_journaled(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": _server(control, persistent_session=False)})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    assert journal_records(service) == []

    await _call(consumer, settings, _ECHO, text="hi")

    assert journal_records(service) == []
    assert launches(control)
    assert _journal_dirs() == []


async def test_backend_server_is_not_journaled(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": _server(control, execute_on="os_backend")})
    backend = IsolatedBackend()
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings, os_backend=backend)

    envelope = await _call(
        consumer, settings, _ECHO, os_backend=backend, text="hi"
    )

    assert _payload(envelope)["text"] == BACKEND_ECHO_TEXT
    assert not any("spawn_launcher" in " ".join(a) for a in backend.argv)
    assert journal_records(service) == []
    assert _journal_dirs() == []


@pytest.mark.parametrize("transport", _PROCESS_TRANSPORTS)
async def test_missing_command_reason_survives_launcher(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, transport
):
    settings = _settings(
        {"a": {"transport": transport, "command": "no-such-xyz"}}
    )
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)

    status = consumer.status_of(_entry(consumer))
    assert "spawn_launcher" in " ".join(_launcher_args(consumer))
    assert status.state == STATE_FAILED
    assert status.reason_code == "command_not_found"
    assert status.reason == "команда 'no-such-xyz' не найдена"
    assert _entry(consumer).last_failure.reason == status.reason


async def test_journal_reason_only_for_server_gone(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    # Свежий сбой exec у ключа объясняет только отказ «сервер завершился,
    # не ответив»; срок и прочие отказы журнал не подменяет.
    settings = _settings({"a": {"transport": "stdio", "command": "no-such"}})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)
    entry = _entry(consumer)
    assert entry.last_failure.code == "command_not_found"

    timeout = service.record_failure(entry, TimeoutError())
    gone = service.record_failure(entry, McpServerGone())

    assert (timeout.code, timeout.reason) == ("timeout", "таймаут подключения")
    assert gone.code == "command_not_found"


async def test_failed_journal_dir_warns_once(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, caplog
):
    # Каталог журнала не создать: серверы работают без учёта, попытка и
    # предупреждение — один раз на сервис, а не на каждую сборку.
    journal_root().parent.parent.mkdir(parents=True, exist_ok=True)
    journal_root().parent.write_text("")
    control = tmp_path / "a.json"
    settings = _settings({"a": _server(control)})
    service = McpService(connect_on_start=True)
    with caplog.at_level("WARNING"):
        consumer = await _ready(mcp_reaper, service, settings)
        for _ in range(3):
            await _ensure(consumer, settings)
            out = await _call(consumer, settings, _ECHO, text="hi")

    warned = [r for r in caplog.records if "журнал процессов" in r.message]
    assert len(warned) == 1
    assert "spawn_launcher" not in " ".join(_launcher_args(consumer))
    assert launches(control) == [_pid_of(out)]
    assert journal_records(service) == []


async def test_server_without_command_does_not_break_view(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    settings = _settings({"a": {"transport": "stdio"}})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)

    view = await asyncio.wait_for(consumer.aview(settings), _CEILING_SEC)

    assert view.status["a"]["status"] == STATE_FAILED
    assert view.servers_cfg["a"]["command"] is None
    assert journal_records(service) == []


async def test_tilde_command_is_resolved_before_launcher(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    control = tmp_path / "a.json"
    real = stdio_cfg(control)
    home = tmp_path / "home"
    script = home / "bin" / "srv"
    script.parent.mkdir(parents=True)
    script.write_text(
        f'#!/bin/sh\nexec "{real["command"]}" "{real["args"][0]}" "$@"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    server = {"transport": "stdio", "command": "~/bin/srv", "env": real["env"]}
    settings = _settings({"a": server})
    service = McpService(connect_on_start=True)
    consumer = await _ready(mcp_reaper, service, settings)

    pid = _pid_of(await _call(consumer, settings, _ECHO, text="hi"))

    args = _launcher_args(consumer)
    assert args[args.index("--") + 1] == str(script)
    assert [r.pid for r in journal_records(service)] == [pid]


async def test_fingerprint_ignores_launcher(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": _server(control)})
    first_service = McpService(connect_on_start=True)
    first = await _ready(mcp_reaper, first_service, settings, "A")
    await _call(first, settings, _ECHO, text="hi")
    second_service = McpService(connect_on_start=False)
    second = await _ready(mcp_reaper, second_service, settings, "B")

    assert _launcher_args(first) != _launcher_args(second)
    assert _entry(first).fingerprint == _entry(second).fingerprint
    assert _entry(second).from_catalog
    assert [t["name"] for t in _entry(second).tools] == ["echo", "nap"]
    assert len(launches(control)) == 1
