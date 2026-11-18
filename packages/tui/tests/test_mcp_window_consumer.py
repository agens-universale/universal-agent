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
    STATE_CACHED,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_FAILED,
    ServerStatus,
)
from gc_testkit import as_coro
from gc_testkit.mcp import exits, launches, set_behavior, stdio_cfg, wait_for
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.tui_support import tui_settings
from gc_tui.services.agent_loop import thread_has_running_loop

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
        #: Серверы, чьё определение в наборе отличается от сборки.
        self.changed: set[str] = set()
        #: Настройки, по которым окно спрашивало `matching_status`.
        self.matched_with: list = []
        self.opened: list[_Consumer] = []
        self.closes = 0
        self.closed = False

    def open_consumer(self, label: str) -> _Consumer:
        # Контракт открытия (`McpService.open_consumer`): из потока без
        # цикла — его стерегут все тесты окна.
        assert not thread_has_running_loop(), "открытие с цикла событий"
        consumer = _Consumer(label)
        self.opened.append(consumer)
        return consumer

    def status(self, consumer) -> dict[str, ServerStatus]:
        assert consumer in self.opened
        return dict(self.statuses)

    def matching_status(self, consumer, settings) -> dict[str, ServerStatus]:
        assert consumer in self.opened
        self.matched_with.append(settings)
        return {
            key: status
            for key, status in self.statuses.items()
            if key not in self.changed
        }

    async def aclose(self, budget: float = 10.0) -> None:
        self.closes += 1
        self.closed = True

    def kill_processes_now(self) -> None:
        self.closed = True


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
    """Подмена `acreate_run_context`: записывает аргументы каждой сборки;
    ``during`` — что сделать посреди сборки (на цикле агента)."""

    def __init__(self, mcp_status: dict | None = None, during=None) -> None:
        self.kwargs: list[dict] = []
        self.settings: list = []
        self.mcp_status = mcp_status
        self.during = during

    async def __call__(self, settings, **kwargs):
        self.kwargs.append(kwargs)
        self.settings.append(settings)
        if self.during is not None:
            self.during()
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
    # Прогрев на монтировании собирает сам — тем же путём, что в окне.
    app = _app(tmp_path, _SRV, service)
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
        # Ни стоп, ни откат потребителя не закрывают; закрывает выход.
        assert service.closes == 0

    consumers = {id(k["mcp_consumer"]) for k in builds.kwargs}
    assert len(builds.kwargs) >= 2
    assert consumers == {id(service.opened[0])}
    assert len(service.opened) == 1
    assert service.closes == 1


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


def test_rebuild_with_changed_server_waits_for_its_status(
    tmp_path, monkeypatch
):
    """Сервер с новым определением под тем же ключом прежнего «подключён»
    в опубликованное поколение не несёт: шапка принимает первый статус
    сервера окончательным и ждёт его настоящего (§5.7, «8/9»)."""
    service = _Service({"srv": _connected("echo")})
    published: list = []

    def _replace_entry() -> None:
        # Поколение уже опубликовано; сверка набора внутри сборки заводит
        # запись нового определения.
        published.append(app._mcp_progress.state())
        if service.changed:
            service.statuses["srv"] = ServerStatus(STATE_CONNECTING)
            service.changed = set()

    builds = _Builds(during=_replace_entry)
    _patch_build(monkeypatch, builds)
    app = _app(tmp_path, _SRV, service)
    app._build_real_executor()  # потребитель окна открыт, srv подключён
    service.changed = {"srv"}  # /profile сменил определение srv

    app._build_real_executor()

    reported, outcome = published[-1]
    assert reported["srv"] is None
    assert outcome is None
    # Отсев — по настройкам этой же сборки, что уходят в сверку набора.
    assert service.matched_with[-1] is builds.settings[-1]


@pytest.mark.asyncio
async def test_rebuild_with_settled_servers_never_shows_loading(
    tmp_path, monkeypatch
):
    """Пересборка, когда серверы уже определились у потребителя (откат
    отправки, первый ход после `/new`): поколение публикуется с их
    статусами, и шапка посреди сборки показывает прежний итог, а не
    «подключаю MCP» (спека §5.7)."""
    mid_build: list = []

    def _capture():
        progress = app._mcp_progress
        mid_build.append((progress, progress.state()))

    _patch_build(monkeypatch, _Builds(during=_capture))
    service = _Service({"srv": _connected("echo")})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(
            asyncio.to_thread(app._build_real_executor), _CEILING_SEC
        )
        app.screen._feed_start_header()
        _spin_header(app)
        await pilot.pause()
        settled = _header_status(app)
        assert settled.startswith("✓") and "MCP 1/1" in settled

        await asyncio.wait_for(
            asyncio.to_thread(app._build_real_executor), _CEILING_SEC
        )
        progress, (servers, outcome) = mid_build[-1]
        assert progress is not mid_build[0][0]
        assert servers["srv"]["status"] == STATE_CONNECTED
        assert outcome is None
        app.screen._brand.feed(progress, servers, outcome)
        await pilot.pause()
        assert _header_status(app) == settled

        app.screen._feed_start_header()
        _spin_header(app)
        await pilot.pause()
        assert _header_status(app) == settled
        assert app._mcp_progress.state()[1] is True


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


def test_cached_server_keeps_generation_open(tmp_path, monkeypatch):
    """`cached` — сервер ещё не подключился: шапка ждёт (§5.7)."""
    _patch_build(monkeypatch, _Builds())
    cached = ServerStatus(
        STATE_CACHED, from_catalog=True, tools=({"name": "echo"},)
    )
    app = _app(tmp_path, _SRV, _Service({"srv": cached}))

    app._build_real_executor()

    reported, outcome = app._mcp_progress.state()
    assert reported["srv"] is None
    assert outcome is None


def test_window_consumer_refused_on_running_loop(tmp_path):
    """С потока, где идёт цикл, потребитель окна не открывается: сервис
    не привязывается к чужому циклу."""
    service = _Service()
    app = _app(tmp_path, _SRV, service)

    async def _open_on_loop():
        app._window_consumer()

    with pytest.raises(RuntimeError, match="рабочего потока сборки"):
        asyncio.run(_open_on_loop())
    assert service.opened == []
    assert app._mcp_consumer is None


@pytest.mark.asyncio
async def test_bad_tool_list_does_not_break_build_or_tick(
    tmp_path, monkeypatch
):
    """Сервер `connected` с негодным описанием тула: сборка отдаёт
    исполнитель, поколение закрыто «собрано», тик шапки не бросает."""
    _patch_build(monkeypatch, _Builds())
    bad = ServerStatus(
        STATE_CONNECTED,
        tools=({"name": "echo", "description": {"text": "x"}},),
    )
    service = _Service({"srv": ServerStatus(STATE_CONNECTING)})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        executor = await asyncio.wait_for(
            asyncio.to_thread(app._build_real_executor), _CEILING_SEC
        )
        service.statuses["srv"] = bad
        app.screen._feed_start_header()
        await pilot.pause()

        assert isinstance(executor, _Executor)
        assert app._mcp_progress.state()[1] is True
        assert app._mcp_status["srv"]["status"] == STATE_FAILED
        assert app._mcp_status["srv"]["reason_code"] == "protocol"


@pytest.mark.asyncio
async def test_overlay_failure_on_tick_keeps_window_copies(
    tmp_path, monkeypatch, caplog
):
    """Сбой наложения статусов на тике шапки — предупреждение в лог, тик
    не бросает, поколение закрыто «собрано», копии окна — те, что дала
    сборка."""
    import logging

    def _broken(*_a, **_k):
        raise TypeError("негодный тул")

    _patch_build(monkeypatch, _Builds())
    service = _Service({"srv": ServerStatus(STATE_CONNECTING)})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(
            asyncio.to_thread(app._build_real_executor), _CEILING_SEC
        )
        snapshot, status = app._mcp_snapshot, app._mcp_status
        monkeypatch.setattr("gc_tui.app.overlay_service_statuses", _broken)
        service.statuses["srv"] = _connected("echo")
        with caplog.at_level(logging.WARNING, logger="gc_tui.app"):
            app.screen._feed_start_header()
        await pilot.pause()

        assert app._mcp_progress.state()[1] is True
        assert app._mcp_snapshot is snapshot
        assert app._mcp_status is status
        assert "не наложены на снимок окна" in caplog.text


@pytest.mark.asyncio
async def test_status_failure_on_tick_keeps_window_copies(
    tmp_path, monkeypatch, caplog
):
    """Сбой чтения статусов сервиса на тике шапки — предупреждение в лог
    с типом и текстом, тик не бросает, копии окна прежние."""
    import logging

    _patch_build(monkeypatch, _Builds())
    service = _Service({"srv": ServerStatus(STATE_CONNECTING)})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(
            asyncio.to_thread(app._build_real_executor), _CEILING_SEC
        )
        snapshot, status = app._mcp_snapshot, app._mcp_status

        def _broken_status(_consumer):
            raise RuntimeError("статус недоступен")

        service.status = _broken_status
        with caplog.at_level(logging.WARNING, logger="gc_tui.app"):
            app.screen._feed_start_header()
        await pilot.pause()

        assert app._mcp_snapshot is snapshot
        assert app._mcp_status is status
        assert "RuntimeError: статус недоступен" in caplog.text


def test_failed_header_step_keeps_successful_build(
    tmp_path, monkeypatch, caplog
):
    """Сбой шага шапки после собранного исполнителя (снимок статусов
    бросил) не делает сборку отказом: исполнитель отдан, итог «собрано»."""
    import logging

    service = _Service()

    def _broken_status(_consumer):
        raise RuntimeError("статус недоступен")

    service.status = _broken_status
    _patch_build(monkeypatch, _Builds())
    app = _app(tmp_path, _SRV, service)

    with caplog.at_level(logging.WARNING, logger="gc_tui.app"):
        executor = app._build_real_executor()

    assert isinstance(executor, _Executor)
    assert app._mcp_progress.state()[1] is True
    assert "статус недоступен" in caplog.text


def test_overlay_uses_build_settings(tmp_path, monkeypatch):
    """Поколение накладывает статусы по настройкам своей сборки: смена
    настроек окна до закрытия поколения на снимок не влияет."""
    _patch_build(monkeypatch, _Builds())
    service = _Service({"srv": ServerStatus(STATE_CONNECTING)})
    app = _app(tmp_path, _SRV, service)
    app._build_real_executor()
    other = tui_settings({"agent.model": "M"})
    monkeypatch.setattr(app, "effective_settings", lambda: other)

    service.statuses["srv"] = _connected("echo")
    app.settle_mcp_progress(app._mcp_progress)

    assert app._mcp_progress.state()[1] is True
    assert app._mcp_snapshot["srv"]["status"] == STATE_CONNECTED
    assert len(app._mcp_snapshot["srv"]["tools"]) == 1


def test_overtaken_build_keeps_newer_copies(tmp_path, monkeypatch):
    """Сборка, которую обогнала более новая (её прогресс уже на
    приложении), копии окна не пишет; статусы внешних серверов своего
    поколения берёт из своего контекста."""
    from gc_tui.services.mcp_progress import McpProgress

    ext_status = {"status": "connected", "reason": None, "tool_count": 1}
    newer = McpProgress()
    seen: list = []

    def _overtake():
        seen.append(app._mcp_progress)
        app._mcp_progress = newer

    _patch_build(
        monkeypatch, _Builds(mcp_status={"ext": ext_status}, during=_overtake)
    )
    servers = {
        "srv": {"transport": "stdio", "command": "srv-mcp"},
        "ext": {"transport": "stdio", "command": "ext-mcp"},
    }
    service = _Service({"srv": _connected("echo")})
    app = _app(tmp_path, {"mcp.servers": servers}, service)
    newer_status = {"srv": {"status": "connected"}}
    newer_snapshot = {"srv": {"status": "connected", "tools": []}}
    app._mcp_status = newer_status
    app._mcp_snapshot = newer_snapshot

    app._build_real_executor()

    (own,) = seen
    assert app._mcp_progress is newer
    assert app._mcp_status is newer_status
    assert app._mcp_snapshot is newer_snapshot
    assert own.state()[1] is True
    assert own.state()[0]["ext"] == ext_status


# ---- настоящий сервис: пересборка не перезапускает сервер ----


_posix_only = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)


class _RealBuilds:
    """Настоящая сборка контекста; записывает потребителя.
    ``cancel_when`` — условие, по которому сборку снимают посреди работы
    (как отмену задачи на цикле агента): в рабочий поток приходит отмена."""

    def __init__(self, real) -> None:
        self.real = real
        self.consumers: list = []
        self.cancel_when = None
        self.contexts: list = []

    async def __call__(self, settings, **kwargs):
        self.consumers.append(kwargs.get("mcp_consumer"))
        if self.cancel_when is None:
            ctx = await self.real(settings, **kwargs)
            self.contexts.append(ctx)
            return ctx
        when, self.cancel_when = self.cancel_when, None
        task = asyncio.ensure_future(self.real(settings, **kwargs))
        await wait_for(when, _CEILING_SEC)
        assert not task.done()
        task.cancel()
        return await task


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
    """Сборка, отменённая посреди ожидания сверки, потребителя не
    закрывает и подключение не прерывает; пересборка (`invalidate_runner`,
    тот же набор серверов) идёт с тем же потребителем и тем же процессом
    сервера; упавшая сборка потребителя не закрывает — следующая сборка
    работает на том же процессе."""
    from gc_core import agent_executor, run_context
    from gc_core.tools.connect_mcp.service import McpService

    control = tmp_path / "srv.json"
    # Каталога нет: сборка ждёт первое подключение сервера.
    set_behavior(control, start_delay=1)
    builds = _RealBuilds(run_context.acreate_run_context)
    monkeypatch.setattr("gc_core.run_context.acreate_run_context", builds)
    service = McpService(connect_on_start=True)
    app = _real_service_app(tmp_path, control, service)
    try:
        builds.cancel_when = lambda: bool(launches(control))
        with pytest.raises(
            (asyncio.CancelledError, concurrent.futures.CancelledError)
        ):
            await _build_off_loop(app)
        assert app._mcp_progress.state()[1] is False
        (pid,) = launches(control)

        await _build_off_loop(app)
        assert app._mcp_progress.state()[1] is True
        assert app._mcp_status["srv"]["status"] == STATE_CONNECTED

        app.invalidate_runner()
        await _build_off_loop(app)

        executor_cls = agent_executor.AgentExecutor
        monkeypatch.setattr(agent_executor, "AgentExecutor", _raise_boom)
        with pytest.raises(RuntimeError, match="boom"):
            await _build_off_loop(app)
        assert app._mcp_progress.state()[1] is False
        monkeypatch.setattr(agent_executor, "AgentExecutor", executor_cls)

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
