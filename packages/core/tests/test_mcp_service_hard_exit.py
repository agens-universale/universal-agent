"""Аварийное гашение, добивание в ``aclose`` и уборка сирот (спека §5.12).

``kill_processes_now`` — синхронно, без цикла и замков, из любого потока:
запрет работы и гашение групп по журналу (с повторным чтением журнала).
``aclose`` после закрытия потребителей добивает по журналу то, что не
уложилось в бюджет, и удаляет каталог журнала. Первая сверка сервиса в фоне
убирает сирот от мёртвых хостов. Процессы настоящие: тестовые серверы,
пускатель журнала и, в тестах жёсткого убийства, отдельный процесс-хост.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from gc_core.tools.connect_mcp import service as service_module
from gc_core.tools.connect_mcp.consumer_connections import session_key_of
from gc_core.tools.connect_mcp.persistent_stdio import PersistentStdioRegistry
from gc_core.tools.connect_mcp.process_journal import (
    ProcessJournal,
    journal_root,
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
    alive,
    connections_of,
    events,
    journal_records,
    launches,
    seed_catalog,
    set_behavior,
    stdio_cfg,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.paths import package_src_dirs
from gc_testkit.settings import make_settings

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="пускатель, журнал и сигналы — только POSIX"
)

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
#: Срок, за который процесс после SIGKILL перестаёт числиться живым.
_DEATH_SEC = 2.0
_ECHO = mcp_tool_full_name("a", "echo")
_NAP = mcp_tool_full_name("a", "nap")


def _settings(servers: dict):
    return make_settings(
        {
            "mcp": {
                "servers": servers,
                "meta": {"discovery_timeout_sec": 20},
            }
        }
    )


def _deaf(control: Path, **extra) -> dict:
    """Сервер, глухой и к SIGTERM, и к концу stdin."""
    set_behavior(control, ignore_term=True, ignore_eof=True)
    return {**stdio_cfg(control), **extra}


def _journal_dirs() -> list[Path]:
    root = journal_root()
    return list(root.iterdir()) if root.exists() else []


async def _ready(service: McpService, settings, label: str = "c"):
    """Потребитель, сверивший набор с ``settings``; закроет его сервис."""
    consumer = service.open_consumer(label)
    build = consumer.next_build()
    await asyncio.wait_for(consumer.aensure(build, settings), _CEILING_SEC)
    return consumer


def _wrapper(view, name: str) -> MCPToolWrapper:
    return MCPToolWrapper(
        name,
        view.servers_cfg,
        view.registry,
        60.0,
        default_use_retry=False,
        caller=view.caller,
    )


def _stdio_sessions(consumer) -> list:
    """Сессии постоянного stdio потребителя в реестре."""
    sessions = PersistentStdioRegistry.instance()._sessions
    return [key for key in list(sessions) if key[0] == consumer.owner]


def _in_flight(control: Path) -> bool:
    return any(line.startswith("req tools/call") for line in events(control))


async def _dead(*pids: int, timeout: float = _DEATH_SEC) -> None:
    await wait_for(lambda: not any(alive(pid) for pid in pids), timeout)


def _spawn_into(journal: ProcessJournal, control: Path) -> subprocess.Popen:
    """Сервер, запущенный пускателем в каталог ``journal`` мимо сессий."""
    cfg = stdio_cfg(control)
    command, args = journal.launch_argv(cfg["command"], cfg["args"], "x")
    return subprocess.Popen(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, **cfg["env"]},
        start_new_session=True,
    )


def _journal_of(service: McpService, tmp_path: Path) -> ProcessJournal:
    """Журнал сервиса, заведённый подстановкой пускателя."""
    cfg = {**stdio_cfg(tmp_path / "probe.json"), "persistent_session": True}
    service.with_launcher("probe", cfg)
    return service._journal


def _reap(*procs: subprocess.Popen) -> None:
    """Добить и пожать процессы, запущенные тестом напрямую."""
    for proc in procs:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(_CEILING_SEC)
        if proc.stdin is not None:
            proc.stdin.close()


class _LoopThread:
    """Цикл сервиса в своём потоке: тест для сервиса — посторонний поток."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, daemon=True
        )
        self._thread.start()

    def submit(self, coro) -> asyncio.Future:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return asyncio.wrap_future(future)

    async def run(self, coro):
        return await asyncio.wait_for(self.submit(coro), _CEILING_SEC)

    async def close(self) -> None:
        await self.run(self._drain())
        self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.to_thread(self._thread.join, _CEILING_SEC)
        self.loop.close()

    @staticmethod
    async def _drain() -> None:
        others = asyncio.all_tasks() - {asyncio.current_task()}
        for task in others:
            task.cancel()
        await asyncio.gather(*others, return_exceptions=True)
        await asyncio.get_running_loop().shutdown_default_executor()


# ---- линия 1: добивание в aclose ----


async def test_aclose_kills_what_did_not_fit_budget(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "deaf.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    await _ready(service, _settings({"a": _deaf(control)}))
    [pid] = launches(control)
    loop = asyncio.get_running_loop()
    started = loop.time()

    await asyncio.wait_for(service.aclose(budget=1), _CEILING_SEC)
    elapsed = loop.time() - started

    await _dead(pid)
    assert _journal_dirs() == []
    bound = 1 + service_module.CLOSE_GRACE_SEC + service_module.KILL_WAIT_SEC
    assert elapsed < bound + 3
    # Повторное закрытие после истёкшего бюджета — сразу: всё добито.
    started = loop.time()
    await asyncio.wait_for(service.aclose(budget=1), _CEILING_SEC)
    assert loop.time() - started < 0.5


@pytest.mark.parametrize("journaled", [True, False])
async def test_view_after_close_creates_no_journal(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, journaled
):
    """Срез закрытого потребителя строит определения для обёрток, но
    каталога журнала заново не заводит — ни после удаления журнала, ни
    когда сервис журнала так и не завёл (тулы из каталога)."""
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    if not journaled:
        seed_catalog(settings, "a", ["echo"])
    service = mcp_reaper.track(McpService(connect_on_start=journaled))
    consumer = await _ready(service, settings)
    assert bool(_journal_dirs()) == journaled

    await asyncio.wait_for(service.aclose(), _CEILING_SEC)
    view = await asyncio.wait_for(consumer.aview(settings), _CEILING_SEC)

    assert _ECHO in view.registry or not journaled
    assert _journal_dirs() == []


# ---- линия 1: аварийное гашение ----


async def test_kill_now_from_foreign_thread_during_call(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    thread = _LoopThread()
    try:
        consumer = await thread.run(_ready(service, settings))
        view = await thread.run(consumer.aview(settings))
        [pid] = launches(control)
        call = thread.submit(_wrapper(view, _NAP).acall(seconds=30))
        await wait_for(lambda: _in_flight(control), _CEILING_SEC)

        started = time.monotonic()
        service.kill_processes_now()

        assert time.monotonic() - started < 5
        await _dead(pid)
        with pytest.raises(McpToolCallError) as caught:
            await asyncio.wait_for(call, _CEILING_SEC)
        assert caught.value.error.message == SERVICE_CLOSED_TEXT
        # Снятый вызов постоянного stdio закрыл свою сессию: поток вызова
        # не поднимет процесс в открытую сессию.
        assert _stdio_sessions(consumer) == []
        late = thread.submit(_wrapper(view, _ECHO).acall(text="hi"))
        with pytest.raises(McpToolCallError) as caught:
            await asyncio.wait_for(late, _CEILING_SEC)
        assert caught.value.error.message == SERVICE_CLOSED_TEXT
        assert launches(control) == [pid]
    finally:
        await thread.run(service.aclose(budget=2))
        await thread.close()


async def test_kill_now_withdraws_calls_in_flight(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Гашение снимает идущие вызовы на цикле сервиса, а не ждёт их
    исхода: повтор вызова (``use_retry``) поднял бы сервер заново. Разовый
    stdio в журнале нет — его вызов кончается только снятием."""
    control = tmp_path / "a.json"
    one_shot = {**stdio_cfg(control), "persistent_session": False}
    settings = _settings({"a": one_shot})
    service = McpService(connect_on_start=True)
    thread = _LoopThread()
    try:
        consumer = await thread.run(_ready(service, settings))
        view = await thread.run(consumer.aview(settings))
        call = thread.submit(_wrapper(view, _NAP).acall(seconds=30))
        await wait_for(lambda: _in_flight(control), _CEILING_SEC)

        service.kill_processes_now()

        with pytest.raises(McpToolCallError) as caught:
            await asyncio.wait_for(call, 5)
        assert caught.value.error.message == SERVICE_CLOSED_TEXT
        assert journal_records(service) == []
    finally:
        await thread.run(service.aclose(budget=2))
        await thread.close()


async def test_no_new_attempts_after_kill(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """После гашения потребитель открыт, но сервис остановлен: попытка,
    вход в ход и серия повторов ничего не начинают.

    Подготовка через приватное: соединение снимается
    ``_drop_connection`` (сервер будто потерян — вход в ход стал бы его
    подключать), серия заводится прямым ``_start_series``.
    """
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, settings)
    connections = connections_of(consumer)
    entry = consumer.servers()["a"]
    [pid] = launches(control)

    service.kill_processes_now()
    await _dead(pid)
    await asyncio.sleep(0.1)  # гашение снимает работу на цикле
    # Сервер будто бы потерян: вход в ход должен был бы его подключать.
    connections._drop_connection(session_key_of(entry))

    assert not consumer.closed
    with pytest.raises(McpServiceClosed):
        connections.start_attempt(entry)
    connections.start_turn(entry)
    connections._start_series(entry)
    assert connections._connects == {}
    assert connections._series == {}
    assert launches(control) == [pid]


async def test_kill_now_rereads_journal(
    mcp_home, tmp_path, monkeypatch, mcp_reaper
):
    """Запуск, записавшийся после первого чтения журнала, тоже гасится."""
    service = McpService(connect_on_start=True)
    journal = _journal_of(service, tmp_path)
    first = _spawn_into(journal, tmp_path / "first.json")
    procs = [first]
    real = ProcessJournal.records
    reads = []

    def records(self):
        found = real(self)
        reads.append(len(found))
        if len(reads) == 1:
            late = _spawn_into(journal, tmp_path / "late.json")
            procs.append(late)
            deadline = time.monotonic() + 5
            while late.pid not in [r.pid for r in real(self)]:
                assert time.monotonic() < deadline, "пускатель не записался"
                time.sleep(0.02)
        return found

    try:
        await wait_for(lambda: len(real(journal)) == 1, 5)
        monkeypatch.setattr(ProcessJournal, "records", records)
        await asyncio.to_thread(service.kill_processes_now)
        await _dead(*(p.pid for p in procs))
        assert len(reads) >= 2
    finally:
        _reap(*procs)


async def test_kill_now_takes_no_service_lock(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Замки сервиса, потребителя и журнала заняты чужим потоком — гашение
    всё равно проходит: оно их не берёт."""
    control = tmp_path / "a.json"
    service = mcp_reaper.track(McpService(connect_on_start=True))
    consumer = await _ready(service, _settings({"a": stdio_cfg(control)}))
    [pid] = launches(control)
    locks = [
        service._lock,
        connections_of(consumer)._lock,
        service._journal._lock,
    ]
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        for lock in locks:
            lock.acquire()
        held.set()
        release.wait(_CEILING_SEC)
        for lock in reversed(locks):
            lock.release()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5)
    killer = threading.Thread(target=service.kill_processes_now, daemon=True)
    try:
        killer.start()
        killer.join(5)
        assert not killer.is_alive()
    finally:
        release.set()
        holder.join(5)
    await _dead(pid)


async def test_foreign_mark_gets_no_signal_on_aclose_and_kill(
    mcp_home, tmp_path, mcp_reaper
):
    """Запись журнала с чужой меткой (pid достался другому процессу):
    ни гашение, ни закрытие сервиса её процесс не трогают."""
    service = McpService(connect_on_start=True)
    journal = _journal_of(service, tmp_path)
    proc = _spawn_into(journal, tmp_path / "foreign.json")
    try:
        await wait_for(lambda: len(journal.records()) == 1, 5)
        [record] = journal.records()
        data = json.loads(record.path.read_text(encoding="utf-8"))
        record.path.write_text(json.dumps({**data, "mark": "bogus"}), "utf-8")

        await asyncio.to_thread(service.kill_processes_now)
        assert alive(proc.pid)
        await asyncio.wait_for(service.aclose(budget=1), _CEILING_SEC)
        assert alive(proc.pid)
        assert _journal_dirs() == []
    finally:
        _reap(proc)


async def test_cancelled_close_still_kills_and_removes_journal(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Задачу закрытия отменили посреди закрытия глухого потребителя
    (свёртка цикла): процессы добиты по журналу, каталога нет, отмена
    осталась отменой."""
    control = tmp_path / "deaf.json"
    service = McpService(connect_on_start=True)
    await _ready(service, _settings({"a": _deaf(control)}))
    [pid] = launches(control)
    caller = asyncio.create_task(service.aclose())
    await wait_for(lambda: service._close_task is not None, _CEILING_SEC)
    await asyncio.sleep(0.3)  # закрытие ждёт эскалации глухого сервера

    service._close_task.cancel()
    await asyncio.wait({service._close_task, caller}, timeout=_CEILING_SEC)

    assert service._close_task.cancelled()
    assert caller.cancelled()
    await _dead(pid)
    assert _journal_dirs() == []
    await _settle_background()


async def test_kill_now_during_aclose(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Гашение из чужого потока одновременно с идущим закрытием: без
    исключений, процессов и каталога журнала нет."""
    control = tmp_path / "deaf.json"
    service = McpService(connect_on_start=True)
    await _ready(service, _settings({"a": _deaf(control)}))
    [pid] = launches(control)
    closing = asyncio.create_task(service.aclose())
    await asyncio.sleep(0.3)  # закрытие ждёт эскалации глухого сервера

    await asyncio.wait_for(
        asyncio.to_thread(service.kill_processes_now), _CEILING_SEC
    )
    await asyncio.wait_for(closing, _CEILING_SEC)

    await _dead(pid)
    assert _journal_dirs() == []


async def test_kill_now_with_busy_service_loop(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Цикл сервиса занят: гашение из чужого потока его не ждёт —
    возвращается в срок гашения и гасит глухой сервер без цикла."""
    control = tmp_path / "deaf.json"
    service = McpService(connect_on_start=True)
    thread = _LoopThread()
    busy, freed = threading.Event(), threading.Event()

    def hog() -> None:
        busy.set()
        time.sleep(4)
        freed.set()

    try:
        await thread.run(_ready(service, _settings({"a": _deaf(control)})))
        [pid] = launches(control)
        thread.loop.call_soon_threadsafe(hog)
        assert busy.wait(5)

        started = time.monotonic()
        service.kill_processes_now()

        assert time.monotonic() - started < service_module.KILL_WAIT_SEC + 2
        assert not freed.is_set()
        await _dead(pid)
    finally:
        await thread.run(service.aclose(budget=2))
        await thread.close()


async def test_attempt_waiting_for_closing_starts_nothing_after_stop(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Попытка ждала прежних закрытий сервера, а сервис тем временем
    остановили: процесса она не запускает. Снятие работы на цикле
    (``_halt``) подменено пустым — проверяется сама проверка попытки:
    закрытие могло разбудить попытку раньше, чем цикл снимет работу."""
    control = tmp_path / "a.json"
    settings = _settings({"a": stdio_cfg(control)})
    seed_catalog(settings, "a", ["echo"])
    service = mcp_reaper.track(McpService(connect_on_start=False))
    consumer = await _ready(service, settings)
    connections = connections_of(consumer)
    entry = consumer.servers()["a"]
    gate = asyncio.Event()
    closing = asyncio.create_task(gate.wait())
    connections._track_closing(closing, "a")
    attempt = connections.start_attempt(entry)
    await asyncio.sleep(0.05)  # попытка ждёт закрытия

    monkeypatch.setattr(service, "_halt", lambda: None)
    service.kill_processes_now()
    gate.set()
    await asyncio.wait({attempt}, timeout=_CEILING_SEC)

    assert attempt.result() is False
    assert launches(control) == []
    assert journal_records(service) == []


async def _settle_background() -> None:
    """Дождаться фоновых задач теста (хвосты закрытий доигрывают)."""
    others = asyncio.all_tasks() - {asyncio.current_task()}
    if others:
        _, pending = await asyncio.wait(others, timeout=_CEILING_SEC)
        assert not pending


# ---- линия 3: уборка сирот при первой сверке ----


def _orphan_journal(tmp_path: Path) -> tuple[subprocess.Popen, Path]:
    """Журнал «мёртвого» хоста (метка хозяина не совпала) с живым
    сервером, глухим к концу stdin."""
    journal = ProcessJournal()
    control = tmp_path / "orphan.json"
    set_behavior(control, ignore_eof=True)
    proc = _spawn_into(journal, control)
    directory = journal._dir
    owner = directory / "owner.json"
    data = json.loads(owner.read_text(encoding="utf-8"))
    owner.write_text(json.dumps({**data, "mark": "bogus"}), "utf-8")
    return proc, directory


async def test_orphans_swept_on_first_ensure(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    proc, directory = _orphan_journal(tmp_path)
    try:
        await wait_for(lambda: launches(tmp_path / "orphan.json"), 5)
        proc.stdin.close()
        service = mcp_reaper.track(McpService(connect_on_start=True))
        consumer = await _ready(service, _settings({}))
        sweep = service._sweep_task
        assert sweep is not None
        await asyncio.wait_for(asyncio.wait({sweep}), _CEILING_SEC)

        await _dead(proc.pid)
        assert not directory.exists()
        # Уборка каталога текущего хоста не заводит, и она одна на сервис.
        assert _journal_dirs() == []
        build = consumer.next_build()
        await asyncio.wait_for(
            consumer.aensure(build, _settings({})), _CEILING_SEC
        )
        assert service._sweep_task is sweep
    finally:
        _reap(proc)


async def test_sweep_failure_is_logged(
    mcp_home, monkeypatch, caplog, mcp_reaper, mcp_owner
):
    def broken(*_args, **_kwargs):
        raise OSError("диск недоступен")

    monkeypatch.setattr(service_module, "sweep_orphans", broken)
    service = mcp_reaper.track(McpService(connect_on_start=True))
    with caplog.at_level(logging.WARNING, logger=service_module.__name__):
        await _ready(service, _settings({}))
        await asyncio.wait_for(
            asyncio.wait({service._sweep_task}), _CEILING_SEC
        )

    assert "диск недоступен" in caplog.text


async def test_close_waits_for_sweep_within_budget(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    release = threading.Event()

    def slow(*_args, **_kwargs) -> int:
        release.wait(_CEILING_SEC)
        return 0

    monkeypatch.setattr(service_module, "sweep_orphans", slow)
    service = McpService(connect_on_start=True)
    await _ready(service, _settings({}))
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        # Уборка висит дольше бюджета: закрытие её не ждёт сверх срока.
        await asyncio.wait_for(service.aclose(budget=0.5), _CEILING_SEC)
        assert loop.time() - started < 0.5 + 1
        assert not service._sweep_task.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.wait({service._sweep_task}), _CEILING_SEC)

    # Быстрая уборка дожидается закрытием.
    release.clear()
    monkeypatch.setattr(
        service_module, "sweep_orphans", lambda *a, **k: time.sleep(0.3)
    )
    service = McpService(connect_on_start=True)
    await _ready(service, _settings({}))
    await asyncio.wait_for(service.aclose(budget=5), _CEILING_SEC)
    assert service._sweep_task.done()


# ---- линии 2 и 3: хост убит SIGKILL ----

#: Процесс-хост: сервис, потребитель, по вызову на каждый сервер, затем
#: «ready» в stdout и ожидание без конца — до SIGKILL от теста.
_HOST = r"""
import asyncio, json, sys

from gc_core.tools.connect_mcp.registry import mcp_tool_full_name
from gc_core.tools.connect_mcp.service import McpService
from gc_core.tools.connect_mcp.tool_wrapper import MCPToolWrapper
from gc_testkit.settings import make_settings

servers = json.loads(sys.argv[1])


async def main():
    settings = make_settings({"mcp": {"servers": servers}})
    service = McpService(connect_on_start=True)
    consumer = service.open_consumer("host")
    await consumer.aensure(consumer.next_build(), settings)
    view = await consumer.aview(settings)
    for key in servers:
        name = mcp_tool_full_name(key, "echo")
        wrapper = MCPToolWrapper(
            name, view.servers_cfg, view.registry, 20.0,
            default_use_retry=False, caller=view.caller,
        )
        await wrapper.acall(text="hi")
    print("ready", flush=True)
    await asyncio.Event().wait()


asyncio.run(main())
"""


async def _killed_host(home: Path, tmp_path: Path, servers: dict) -> None:
    """Запустить хост, дождаться вызовов и убить его SIGKILL."""
    env = dict(os.environ, GC_HOME=str(home))
    src = [str(d) for d in package_src_dirs()]
    env["PYTHONPATH"] = os.pathsep.join(
        src + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    with open(tmp_path / "host.err", "wb") as err:
        host = subprocess.Popen(
            [sys.executable, "-c", _HOST, json.dumps(servers)],
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
    finally:
        os.killpg(host.pid, signal.SIGKILL)
        host.wait(_CEILING_SEC)
        host.stdout.close()


async def test_servers_exit_when_host_is_killed_hard(
    mcp_home, tmp_path, mcp_reaper
):
    """Линия 2: процессные серверы выходят сами по закрытию stdin."""
    plain, stream = tmp_path / "a.json", tmp_path / "b.json"
    servers = {
        "a": stdio_cfg(plain),
        "b": {**stdio_cfg(stream), "transport": "stream_stdio"},
    }

    await _killed_host(mcp_home, tmp_path, servers)

    pids = launches(plain) + launches(stream)
    assert len(pids) == 2
    await _dead(*pids, timeout=10)


async def test_deaf_server_outlives_killed_host_until_next_start(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    """Сервер глух к закрытию stdin: хост убит, сервер жив — его гасит
    уборка сирот при первой сверке следующего запуска (линия 3)."""
    control = tmp_path / "a.json"
    set_behavior(control, ignore_eof=True)

    await _killed_host(mcp_home, tmp_path, {"a": stdio_cfg(control)})

    [pid] = launches(control)
    await asyncio.sleep(1)
    assert alive(pid)
    assert len(_journal_dirs()) == 1
    service = mcp_reaper.track(McpService(connect_on_start=True))
    await _ready(service, _settings({}))
    await asyncio.wait_for(asyncio.wait({service._sweep_task}), _CEILING_SEC)

    await _dead(pid)
    assert _journal_dirs() == []
