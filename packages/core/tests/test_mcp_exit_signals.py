"""SIGTERM хоста гасит процессы MCP аварийным путём (спека §5.12).

Обработчик ``kill_mcp_on_sigterm``: диспозиция по умолчанию первым делом
(повторный SIGTERM завершает сразу), гашение по журналу сервиса и тот же
сигнал себе — процесс умирает от SIGTERM, как без обработчика. Цикл на
главном потоке будит сам сигнал (wakeup-fd): сигнал, принятый другим
потоком, иначе ждал бы следующего события цикла. Хост — отдельный процесс,
серверы настоящие.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from gc_core.tools.connect_mcp.exit_signals import kill_mcp_on_sigterm
from gc_core.tools.connect_mcp.service import McpService
from gc_testkit.mcp import (
    alive,
    events,
    launches,
    set_behavior,
    stdio_cfg,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_home, mcp_reaper
from gc_testkit.paths import package_src_dirs

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="сигналы и журнал процессов — только POSIX"
)

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
#: Срок выхода хоста по SIGTERM: гашение укладывается в 2 с.
_EXIT_SEC = 10.0
#: Пауза после «ready», за которую цикл хоста засыпает в ожидании событий.
_ASLEEP_SEC = 0.5

#: Процесс-хост: сервис, потребитель, по вызову ``nap(60)`` на каждый
#: сервер, обработчик SIGTERM, «ready» в stdout и ожидание без конца.
#: Режим ``foreign``: главный поток блокирует SIGTERM, его принимает другой
#: поток — сон цикла сигнал не прерывает, будит только wakeup-fd. Других
#: событий у цикла нет: без пробуждения хост ждал бы вечно.
_HOST = r"""
import asyncio, json, signal, sys, threading

from gc_core.tools.connect_mcp.exit_signals import kill_mcp_on_sigterm
from gc_core.tools.connect_mcp.registry import mcp_tool_full_name
from gc_core.tools.connect_mcp.service import McpService
from gc_core.tools.connect_mcp.tool_wrapper import MCPToolWrapper
from gc_testkit.settings import make_settings

servers = json.loads(sys.argv[1])
mode = sys.argv[2]


async def nap_on_each_server(service):
    settings = make_settings({"mcp": {"servers": servers}})
    consumer = service.open_consumer("host")
    await consumer.aensure(consumer.next_build(), settings)
    view = await consumer.aview(settings)
    calls = []
    for key in servers:
        wrapper = MCPToolWrapper(
            mcp_tool_full_name(key, "nap"), view.servers_cfg, view.registry,
            120.0, default_use_retry=False, caller=view.caller,
        )
        calls.append(asyncio.ensure_future(wrapper.acall(seconds=60)))
    return calls


async def main():
    service = McpService(connect_on_start=True)
    # Без серверов сверки нет: её фоновая уборка сирот, кончившись после
    # сигнала, разбудила бы цикл сама.
    calls = await nap_on_each_server(service) if servers else []
    kill_mcp_on_sigterm(service)
    if mode == "foreign":
        threading.Thread(target=threading.Event().wait, daemon=True).start()
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    print("ready", flush=True)
    await asyncio.Event().wait()


asyncio.run(main())
"""


@contextlib.asynccontextmanager
async def _host(home: Path, tmp_path: Path, servers: dict, mode="main"):
    """Хост, дошедший до «ready»; на выходе добит и пожат."""
    env = dict(os.environ, GC_HOME=str(home))
    src = [str(d) for d in package_src_dirs()]
    env["PYTHONPATH"] = os.pathsep.join(
        src + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    with open(tmp_path / "host.err", "wb") as err:
        host = subprocess.Popen(
            [sys.executable, "-c", _HOST, json.dumps(servers), mode],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            env=env,
            start_new_session=True,
        )
    try:
        line = await asyncio.wait_for(
            asyncio.to_thread(host.stdout.readline), 60
        )
        assert line.strip() == b"ready", (tmp_path / "host.err").read_text()
        yield host
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(host.pid, signal.SIGKILL)
        await asyncio.to_thread(host.wait, _CEILING_SEC)
        host.stdout.close()


def _in_flight(control: Path) -> bool:
    return any(line.startswith("req tools/call") for line in events(control))


async def _exit_status(host: subprocess.Popen, timeout: float) -> int:
    return await asyncio.to_thread(host.wait, timeout)


def _wakeup_fd() -> int:
    """Текущий wakeup-fd процесса (``-1`` — не поставлен)."""
    fd = signal.set_wakeup_fd(-1)
    signal.set_wakeup_fd(fd)
    return fd


async def test_sigterm_kills_mcp_and_dies_by_signal(
    mcp_home, tmp_path, mcp_reaper
):
    """Сервер глух к концу stdin: смерть хоста сама его не погасит —
    гасит обработчик, а хост умирает от SIGTERM."""
    control = tmp_path / "a.json"
    set_behavior(control, ignore_eof=True)
    servers = {"a": stdio_cfg(control)}
    async with _host(mcp_home, tmp_path, servers) as host:
        await wait_for(lambda: _in_flight(control), _CEILING_SEC)
        [pid] = launches(control)

        host.send_signal(signal.SIGTERM)

        assert await _exit_status(host, _EXIT_SEC) == -signal.SIGTERM
        await wait_for(lambda: not alive(pid), 2.0)


async def test_second_sigterm_exits_at_once(mcp_home, tmp_path, mcp_reaper):
    """Сервер глух к SIGTERM: первое гашение ждёт его до 2 с, второй
    SIGTERM завершает хост сразу."""
    control = tmp_path / "a.json"
    set_behavior(control, ignore_term=True)
    servers = {"a": stdio_cfg(control)}
    async with _host(mcp_home, tmp_path, servers) as host:
        await wait_for(lambda: _in_flight(control), _CEILING_SEC)

        host.send_signal(signal.SIGTERM)
        await asyncio.sleep(0.5)
        assert host.poll() is None  # первое гашение ещё ждёт сервер
        host.send_signal(signal.SIGTERM)

        assert await _exit_status(host, 1.5) == -signal.SIGTERM


@pytest.mark.parametrize("attempt", range(5))
async def test_sigterm_taken_by_another_thread_wakes_the_loop(
    mcp_home, tmp_path, attempt
):
    """Цикл спит без событий, SIGTERM принимает другой поток: хост
    всё равно умирает от SIGTERM в срок — сигнал будит цикл."""
    del attempt
    async with _host(mcp_home, tmp_path, {}, mode="foreign") as host:
        # Сигнал, пойманный, пока главный поток ещё исполняет код, обработчик
        # получил бы и без пробуждения: даём циклу заснуть в ожидании.
        await asyncio.sleep(_ASLEEP_SEC)
        host.send_signal(signal.SIGTERM)

        assert await _exit_status(host, _EXIT_SEC) == -signal.SIGTERM


async def test_handler_is_restored_and_noop_off_main_thread(
    mcp_home, monkeypatch
):
    service = McpService(connect_on_start=False)
    before = signal.getsignal(signal.SIGTERM)
    fd_before = _wakeup_fd()

    restore = kill_mcp_on_sigterm(service)
    assert signal.getsignal(signal.SIGTERM) != before
    assert _wakeup_fd() != fd_before  # цикл теста будит сигнал
    restore()

    assert signal.getsignal(signal.SIGTERM) == before
    assert _wakeup_fd() == fd_before

    calls: list[tuple] = []
    monkeypatch.setattr(signal, "signal", lambda *args: calls.append(args))
    restores: list = []
    worker = threading.Thread(
        target=lambda: restores.append(kill_mcp_on_sigterm(service))
    )
    worker.start()
    worker.join(_CEILING_SEC)
    [restore_off_main] = restores
    restore_off_main()
    assert calls == []
