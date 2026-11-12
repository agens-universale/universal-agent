"""Окно TUI — потребитель сервиса MCP (спека §5.1, §5.7, §6).

Приложение держит один сервис и одного потребителя «окно»: каждая сборка
исполнителя (прогрев, пересборка после `/stop`, отката отправки, смены
профиля) получает того же потребителя, отказ или отмена сборки его не
закрывают. Стартовая шапка ждёт, пока серверы поколения не определятся по
статусам потребителя. Логика шапки — на двойнике сервиса; главное свойство
(пересборка не перезапускает сервер) — на настоящем сервисе и тестовом
stdio-сервере.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
import time

import pytest
from gc_core.tools.connect_mcp.consumer_status import (
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_FAILED,
    ServerStatus,
)
from gc_testkit import as_coro
from gc_testkit.mcp import exits, launches, stdio_cfg
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
_SRV = {"mcp.servers": {"srv": {"transport": "stdio", "command": "srv-mcp"}}}
_FAILED_REASON = "команда не найдена"


class _Consumer:
    def __init__(self, label: str) -> None:
        self.label = label


class _Service:
    """Двойник сервиса: управляемый снимок статусов и счётчики."""

    def __init__(self, statuses: dict | None = None) -> None:
        self.statuses: dict[str, ServerStatus] = dict(statuses or {})
        self.opened: list[_Consumer] = []
        self.closes = 0

    def open_consumer(self, label: str) -> _Consumer:
        consumer = _Consumer(label)
        self.opened.append(consumer)
        return consumer

    def status(self, consumer) -> dict[str, ServerStatus]:
        assert consumer in self.opened
        return dict(self.statuses)

    async def aclose(self, budget: float = 10.0) -> None:
        self.closes += 1


class _Ctx:
    os_backend = None
    tools_data: dict = {}
    logging_path = "/tmp/agent_main_test.log"
    registered_tool_names = None

    def __init__(self, mcp_status: dict | None = None) -> None:
        self.mcp_status = dict(mcp_status or {})


class _Executor:
    def __init__(self, ctx) -> None:
        self._ctx = ctx

    def stop_run(self) -> None:
        pass


class _Builds:
    """Подмена `acreate_run_context`: записывает аргументы каждой сборки."""

    def __init__(self, mcp_status: dict | None = None) -> None:
        self.kwargs: list[dict] = []
        self.mcp_status = mcp_status

    async def __call__(self, _settings, **kwargs):
        self.kwargs.append(kwargs)
        return _Ctx(self.mcp_status)


def _connected(*tools: str) -> ServerStatus:
    return ServerStatus(
        STATE_CONNECTED, tools=tuple({"name": name} for name in tools)
    )


def _patch_build(monkeypatch, build) -> None:
    monkeypatch.setattr(
        "gc_core.config_modules.auth.build_auth_connector", lambda cfg: None
    )
    monkeypatch.setattr("gc_core.run_context.acreate_run_context", build)
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", _Executor)


def _app(tmp_path, config, service=None, *, mounted: bool = False):
    from gc_tui.app import TuiApp

    kwargs = {} if service is None else {"mcp_service": service}
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M", **config}),
        **kwargs,
    )
    if mounted:
        # Прогрев на монтировании не строит: сборку тест зовёт сам, из
        # потока без цикла — как рабочий поток раннера.
        app._executor_factory = lambda: object()
    return app


def _header_status(app) -> str:
    from gc_tui.widgets.start_header import StartHeader

    header = app.screen.query_one("#brand-block", StartHeader)
    lines = header.render().laid_out(header.content_region.width).plain
    return next(
        ln for ln in lines.splitlines() if ln.startswith(("•", "✓", "⚠"))
    )


def _spin_header(app, ticks: int = 40) -> None:
    from gc_tui.widgets.start_header import StartHeader

    header = app.screen.query_one("#brand-block", StartHeader)
    for _ in range(ticks):
        header._tick()


# ---- потребитель окна ----


def test_every_build_gets_the_same_consumer(tmp_path, monkeypatch):
    builds = _Builds()
    _patch_build(monkeypatch, builds)
    service = _Service()
    app = _app(tmp_path, _SRV, service)

    app._build_real_executor()
    app.invalidate_runner()
    app._build_real_executor()

    first, second = builds.kwargs
    assert first["mcp_consumer"] is second["mcp_consumer"]
    assert first["mcp_consumer"] is service.opened[0]
    assert "on_progress" not in first
    assert len(service.opened) == 1
    assert service.opened[0].label == "window"


def test_concurrent_builds_open_one_consumer(tmp_path, monkeypatch):
    """Прогрев и `/profile` собирают одновременно: потребитель один."""
    builds = _Builds()
    _patch_build(monkeypatch, builds)
    service = _Service()
    opened = service.open_consumer

    def _slow_open(label):
        time.sleep(0.05)
        return opened(label)

    service.open_consumer = _slow_open
    app = _app(tmp_path, _SRV, service)
    workers = [
        threading.Thread(target=app._build_real_executor) for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(_CEILING_SEC)

    assert len(builds.kwargs) == 2
    assert len(service.opened) == 1
    assert {id(k["mcp_consumer"]) for k in builds.kwargs} == {
        id(service.opened[0])
    }


@pytest.mark.asyncio
async def test_stop_and_send_undo_keep_consumer(tmp_path, monkeypatch):
    """`/stop` и откат отправки по Esc идут общим `_stop_and_replace_runner`:
    новый раннер собирается с тем же потребителем, его никто не закрывает."""
    builds = _Builds()
    _patch_build(monkeypatch, builds)
    service = _Service()
    app = _app(tmp_path, _SRV, service, mounted=True)
    app._executor_factory = None
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        first = app.get_runner()
        await asyncio.wait_for(
            asyncio.to_thread(first._ensure_executor), _CEILING_SEC
        )
        app.screen._stop_and_replace_runner()
        second = app.get_runner()
        assert second is not first
        await asyncio.wait_for(
            asyncio.to_thread(second._ensure_executor), _CEILING_SEC
        )

    consumers = {id(k["mcp_consumer"]) for k in builds.kwargs}
    assert len(builds.kwargs) >= 2
    assert consumers == {id(service.opened[0])}
    assert len(service.opened) == 1
    assert service.closes == 0


def test_main_passes_service_to_app(monkeypatch, tmp_path):
    from gc_core.tools.connect_mcp.service import McpService
    from gc_tui import app as tui_app

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "backend:\n"
        "  api_mode: chat_completions\n"
        "  completions_url: http://127.0.0.1:9\n"
        "paths:\n"
        f'  threads_data_path: "{threads_dir.as_posix()}"\n',
        encoding="utf-8",
    )
    called: dict = {}

    class _FakeApp:
        def __init__(self, **kw):
            called.update(kw)

        def set_config_path(self, path):
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons",
        lambda cfg, **kw: None,
    )
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons",
        lambda: None,
    )
    from gc_tui import main

    assert main(argv=["--config", str(cfg_path)]) == 0
    service = called.get("mcp_service")
    assert isinstance(service, McpService)
    assert service.connect_on_start is True


def test_app_without_service_creates_its_own(tmp_path):
    from gc_core.tools.connect_mcp.service import McpService

    app = _app(tmp_path, {})
    assert isinstance(app._mcp_service, McpService)
    assert app._mcp_service.connect_on_start is True
    assert app._mcp_consumer is None


# ---- стартовая шапка по статусам потребителя ----


@pytest.mark.asyncio
async def test_header_keeps_loading_while_connecting(tmp_path, monkeypatch):
    _patch_build(monkeypatch, _Builds())
    service = _Service({"srv": ServerStatus(STATE_CONNECTING)})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(
            asyncio.to_thread(app._build_real_executor), _CEILING_SEC
        )
        app.screen._feed_start_header()
        await pilot.pause()
        assert app._mcp_progress.state()[1] is None
        _spin_header(app)
        await pilot.pause()
        assert "подключаю MCP 0/1" in _header_status(app)


@pytest.mark.asyncio
async def test_header_settles_when_connected(tmp_path, monkeypatch):
    _patch_build(monkeypatch, _Builds())
    service = _Service({"srv": ServerStatus(STATE_CONNECTING)})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(
            asyncio.to_thread(app._build_real_executor), _CEILING_SEC
        )
        service.statuses["srv"] = _connected("echo")
        app.screen._feed_start_header()
        await pilot.pause()
        assert app._mcp_progress.state()[1] is True
        _spin_header(app)
        await pilot.pause()
        assert "MCP 1/1" in _header_status(app)
        assert _header_status(app).startswith("✓")


def test_already_connected_are_counted_at_once(tmp_path, monkeypatch):
    _patch_build(monkeypatch, _Builds())
    names = [f"s{i}" for i in range(9)]
    servers = {n: {"transport": "stdio", "command": f"{n}-mcp"} for n in names}
    statuses = {n: _connected("echo") for n in names[:8]}
    statuses[names[8]] = ServerStatus(STATE_CONNECTING)
    app = _app(tmp_path, {"mcp.servers": servers}, _Service(statuses))

    app._build_real_executor()

    reported, outcome = app._mcp_progress.state()
    assert {n for n, st in reported.items() if st is not None} == set(
        names[:8]
    )
    assert outcome is None


def test_rebuild_without_changes_settles_immediately(tmp_path, monkeypatch):
    _patch_build(monkeypatch, _Builds())
    app = _app(tmp_path, _SRV, _Service({"srv": _connected("echo")}))

    app._build_real_executor()

    reported, outcome = app._mcp_progress.state()
    assert outcome is True
    assert reported["srv"]["status"] == STATE_CONNECTED


@pytest.mark.asyncio
async def test_final_failed_status_reaches_startup_notices(
    tmp_path, monkeypatch
):
    _patch_build(monkeypatch, _Builds())
    service = _Service({"srv": ServerStatus(STATE_CONNECTING)})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(
            asyncio.to_thread(app._build_real_executor), _CEILING_SEC
        )
        service.statuses["srv"] = ServerStatus(
            STATE_FAILED,
            reason_code="command_not_found",
            reason=_FAILED_REASON,
        )
        app.screen._feed_start_header()
        await pilot.pause()

        assert app._mcp_progress.state()[1] is True
        assert app._mcp_progress.state()[0]["srv"]["reason"] == (
            _FAILED_REASON
        )
        assert app._mcp_status["srv"]["status"] == STATE_FAILED
        assert app._mcp_status["srv"]["reason"] == _FAILED_REASON


def test_external_servers_report_build_status(tmp_path, monkeypatch):
    """Сервер вне набора потребителя (исполняется в бэкенде, §5.10)
    отчитывается статусом сборки сразу после неё."""
    ext_status = {"status": "connected", "reason": None, "tool_count": 2}
    # Сервер потребителя сборка тоже отдаёт статусом среза (`connecting`):
    # он не должен попасть в шапку — у глазка первый статус окончателен.
    srv_status = {"status": "connecting", "reason": None, "tool_count": 0}
    _patch_build(
        monkeypatch,
        _Builds(mcp_status={"ext": ext_status, "srv": srv_status}),
    )
    servers = {
        "srv": {"transport": "stdio", "command": "srv-mcp"},
        "ext": {"transport": "stdio", "command": "ext-mcp"},
    }
    service = _Service({"srv": ServerStatus(STATE_CONNECTING)})
    app = _app(tmp_path, {"mcp.servers": servers}, service)

    app._build_real_executor()

    reported, outcome = app._mcp_progress.state()
    assert reported["ext"] == ext_status
    assert reported["srv"] is None
    assert outcome is None


def test_snapshot_after_settle_has_tools(tmp_path, monkeypatch):
    from gc_core.tools.connect_mcp.registry import mcp_tool_full_name

    _patch_build(monkeypatch, _Builds())
    app = _app(tmp_path, _SRV, _Service({"srv": _connected("echo", "nap")}))

    app._build_real_executor()

    tools = {t["name"] for t in app._mcp_snapshot["srv"]["tools"]}
    assert tools == {
        mcp_tool_full_name("srv", "echo"),
        mcp_tool_full_name("srv", "nap"),
    }
    assert app._mcp_snapshot["srv"]["status"] == STATE_CONNECTED
    assert app._mcp_status["srv"]["tool_count"] == 2


def test_failed_build_still_finishes_false(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    _patch_build(monkeypatch, as_coro(_boom))
    app = _app(tmp_path, _SRV)
    with pytest.raises(RuntimeError, match="boom"):
        app._build_real_executor()
    assert app._mcp_progress.state()[1] is False


def test_build_without_servers_finishes_true(tmp_path, monkeypatch):
    _patch_build(monkeypatch, _Builds())
    app = _app(tmp_path, {})
    app._build_real_executor()
    assert app._mcp_progress.state()[1] is True


# ---- настоящий сервис: пересборка не перезапускает сервер ----


_posix_only = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)


class _RealBuilds:
    """Настоящая сборка контекста; записывает потребителя. ``cancel_next``
    снимает задачу сборки на цикле агента после сверки потребителя — как
    отмена сборки на выходе: в рабочий поток приходит отмена."""

    def __init__(self, real) -> None:
        self.real = real
        self.consumers: list = []
        self.cancel_next = False
        self.contexts: list = []

    async def __call__(self, settings, **kwargs):
        self.consumers.append(kwargs.get("mcp_consumer"))
        ctx = await self.real(settings, **kwargs)
        self.contexts.append(ctx)
        if self.cancel_next:
            self.cancel_next = False
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        return ctx


def _real_service_app(tmp_path, control, service):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "mcp.servers": {"srv": stdio_cfg(control)}}
        ),
        mcp_service=service,
    )


async def _build_off_loop(app) -> None:
    """Сборка из потока без цикла — как рабочий поток раннера."""
    await asyncio.wait_for(
        asyncio.to_thread(app._build_real_executor), _CEILING_SEC
    )


def _shutdown(app, service, builds) -> None:
    from gc_core.utils.logging_module import release_run_logger

    for ctx in builds.contexts:
        release_run_logger(ctx.logger)
    app.agent_loop.run(service.aclose(budget=2.0), timeout=_CEILING_SEC)
    app.agent_loop.shutdown()


@_posix_only
async def test_rebuild_and_failed_builds_keep_server_process(
    tmp_path, monkeypatch, mcp_home, mcp_reaper, mcp_owner
):
    """Пересборка (`invalidate_runner`, та же смена серверов) идёт с тем же
    потребителем и тем же процессом сервера; упавшая и отменённая сборки
    потребителя не закрывают — следующая сборка работает на том же
    процессе."""
    from gc_core import agent_executor, run_context
    from gc_core.tools.connect_mcp.service import McpService

    control = tmp_path / "srv.json"
    builds = _RealBuilds(run_context.acreate_run_context)
    monkeypatch.setattr("gc_core.run_context.acreate_run_context", builds)
    service = McpService(connect_on_start=True)
    app = _real_service_app(tmp_path, control, service)
    try:
        await _build_off_loop(app)
        assert app._mcp_progress.state()[1] is True
        (pid,) = launches(control)

        app.invalidate_runner()
        await _build_off_loop(app)

        executor_cls = agent_executor.AgentExecutor
        monkeypatch.setattr(agent_executor, "AgentExecutor", _raise_boom)
        with pytest.raises(RuntimeError, match="boom"):
            await _build_off_loop(app)
        assert app._mcp_progress.state()[1] is False
        monkeypatch.setattr(agent_executor, "AgentExecutor", executor_cls)

        builds.cancel_next = True
        with pytest.raises(
            (asyncio.CancelledError, concurrent.futures.CancelledError)
        ):
            await _build_off_loop(app)
        assert app._mcp_progress.state()[1] is False

        await _build_off_loop(app)
        assert app._mcp_progress.state()[1] is True
        assert app._mcp_status["srv"]["status"] == STATE_CONNECTED
        consumer = app._mcp_consumer
        assert all(c is consumer for c in builds.consumers)
        assert len(builds.consumers) == 5
        assert not consumer.closed
        assert launches(control) == [pid]
        assert exits(control) == []
    finally:
        await asyncio.to_thread(_shutdown, app, service, builds)


def _raise_boom(_ctx):
    raise RuntimeError("boom")


@_posix_only
async def test_warm_build_on_mount_binds_service_to_agent_loop(
    tmp_path, mcp_home, mcp_reaper, mcp_owner
):
    """Прогрев на монтировании собирает с потребителем окна из рабочего
    потока: сервис привязан к циклу агента, не к циклу интерфейса, и
    сервер подключён; шапка закрывает поколение по статусу потребителя."""
    from gc_core.tools.connect_mcp.service import McpService

    control = tmp_path / "srv.json"
    service = McpService(connect_on_start=True)
    app = _real_service_app(tmp_path, control, service)
    async with app.run_test(size=(120, 40)) as pilot:
        try:
            await _wait_outcome(app, pilot)
            assert app._mcp_progress.state()[1] is True
            assert service._loop is app.agent_loop._loop
            assert app._mcp_status["srv"]["status"] == STATE_CONNECTED
            assert len(launches(control)) == 1
        finally:
            await asyncio.wait_for(
                asyncio.to_thread(
                    app.agent_loop.run, service.aclose(budget=2.0)
                ),
                _CEILING_SEC,
            )


async def _wait_outcome(app, pilot) -> None:
    """Дождаться итога поколения шапки (опрос с потолком)."""

    async def _poll() -> None:
        while True:
            progress = app._mcp_progress
            if progress is not None and progress.state()[1] is not None:
                return
            await pilot.pause(0.05)

    await asyncio.wait_for(_poll(), _CEILING_SEC)
