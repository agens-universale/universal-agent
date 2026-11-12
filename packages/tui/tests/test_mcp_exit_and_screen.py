"""Окно TUI: закрытие сервиса MCP на выходе и экран `/mcp` (спека §5.7,
§5.12).

Выход закрывает сервис на цикле агента между освобождением раннера и
остановкой цикла, с потолком; сбой и срок закрытия выход не держат.
Копии статусов окна (`_mcp_snapshot`, `_mcp_status`) следуют за сервисом
и после итога шапки; экран `/mcp` показывает четыре состояния.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest
from gc_core.tools.connect_mcp import service as service_module
from gc_core.tools.connect_mcp.consumer_status import (
    STATE_CACHED,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_FAILED,
    ServerStatus,
)
from gc_testkit.mcp import alive, launches, set_behavior, stdio_cfg, wait_for
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.tui_support import tui_settings
from gc_tui.services.agent_loop import THREAD_NAME, thread_has_running_loop
from screen_text import mcp_text

pytest.importorskip("textual")

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
#: Остановка цикла агента на выходе (``AgentLoop.shutdown``).
_LOOP_SHUTDOWN_SEC = 5.0
_SRV = {"mcp.servers": {"srv": {"transport": "stdio", "command": "srv-mcp"}}}


class _Consumer:
    def __init__(self, label: str) -> None:
        self.label = label


class _Service:
    """Двойник сервиса: управляемый снимок статусов, счётчики, порядок."""

    def __init__(self, statuses: dict | None = None, events=None) -> None:
        self.statuses: dict[str, ServerStatus] = dict(statuses or {})
        self.opened: list[_Consumer] = []
        self.closes = 0
        self.closed = False
        self.events = events if events is not None else []
        self.on_close = None

    def open_consumer(self, label: str) -> _Consumer:
        assert not thread_has_running_loop(), "открытие с цикла событий"
        consumer = _Consumer(label)
        self.opened.append(consumer)
        return consumer

    def status(self, consumer) -> dict[str, ServerStatus]:
        assert consumer in self.opened
        return dict(self.statuses)

    async def aclose(self, budget: float = 10.0) -> None:
        self.closes += 1
        self.closed = True
        self.events.append("mcp_close")
        if self.on_close is not None:
            await self.on_close()

    def kill_processes_now(self) -> None:
        self.closed = True
        self.events.append("mcp_kill")


def _opened(service: _Service) -> _Consumer:
    """Потребитель окна, будто его открыла сборка (тест идёт на цикле,
    а открытие с цикла двойник не принимает)."""
    consumer = _Consumer("window")
    service.opened.append(consumer)
    return consumer


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


async def _build(_settings, **_kwargs):
    return _Ctx()


def _patch_build(monkeypatch) -> None:
    monkeypatch.setattr(
        "gc_core.config_modules.auth.build_auth_connector", lambda cfg: None
    )
    monkeypatch.setattr("gc_core.run_context.acreate_run_context", _build)
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


def _connected(*tools: str) -> ServerStatus:
    return ServerStatus(
        STATE_CONNECTED, tools=tuple({"name": name} for name in tools)
    )


async def _build_off_loop(app) -> None:
    await asyncio.wait_for(
        asyncio.to_thread(app._build_real_executor), _CEILING_SEC
    )


def _loop_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == THREAD_NAME]


# ---- закрытие на выходе ----


class _Runner:
    def __init__(self, events: list) -> None:
        self.events = events

    def stop(self) -> None:
        self.events.append("stop")

    def wait(self, _timeout) -> None:
        self.events.append("wait")

    def release(self) -> None:
        self.events.append("release")


class _Loop:
    """Двойник цикла агента: корутину исполняет сам, пишет порядок."""

    def __init__(self, events: list) -> None:
        self.events = events
        self.timeouts: list = []

    def run(self, coro, timeout=None):
        self.timeouts.append(timeout)
        return asyncio.run(coro)

    def shutdown(self, timeout: float = 5.0) -> None:
        self.events.append("loop_shutdown")


async def test_unmount_closes_service_between_runner_and_loop(tmp_path):
    events: list = []
    service = _Service(events=events)
    app = _app(tmp_path, _SRV, service)
    app._mcp_consumer = _opened(service)
    app.active_runner = _Runner(events)
    loop = _Loop(events)
    app.agent_loop = loop

    await asyncio.wait_for(app.on_unmount(), _CEILING_SEC)

    assert events == [
        "stop",
        "wait",
        "release",
        "mcp_close",
        "loop_shutdown",
        "mcp_kill",
    ]
    # Потолок выше худшего возврата `aclose`: бюджет + отсрочка +
    # добивание по журналу, с запасом на очередь цикла агента.
    (timeout,) = loop.timeouts
    worst = (
        service_module.CLOSE_BUDGET_SEC
        + service_module.CLOSE_GRACE_SEC
        + service_module.KILL_WAIT_SEC
    )
    assert timeout > worst


async def test_unmount_without_consumer_does_not_start_loop(tmp_path):
    """Потребитель не открывался — закрывать нечего: поток цикла агента
    ради закрытия не поднимается."""
    service = _Service()
    app = _app(tmp_path, _SRV, service)

    await asyncio.wait_for(app.on_unmount(), _CEILING_SEC)

    assert service.closes == 0
    assert app.agent_loop.thread is None


@pytest.mark.parametrize("failure", ["raises", "hangs"])
async def test_unmount_survives_close_failure(
    tmp_path, monkeypatch, caplog, failure
):
    """`aclose` бросает или висит дольше потолка: выход идёт дальше,
    цикл агента останавливается, причина — в лог."""
    import logging

    from gc_tui import app as app_module

    monkeypatch.setattr(app_module, "MCP_CLOSE_TIMEOUT_SEC", 0.2)
    service = _Service()

    async def _raise():
        raise RuntimeError("закрытие сломано")

    async def _hang():
        await asyncio.sleep(60)

    service.on_close = _raise if failure == "raises" else _hang
    app = _app(tmp_path, _SRV, service)
    app._mcp_consumer = _opened(service)
    shutdowns: list = []
    real_shutdown = app.agent_loop.shutdown

    def _shutdown(timeout: float = 5.0) -> None:
        shutdowns.append(timeout)
        real_shutdown(timeout)

    app.agent_loop.shutdown = _shutdown
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="gc_tui.app"):
        await asyncio.wait_for(app.on_unmount(), _CEILING_SEC)

    assert service.closes == 1
    assert len(shutdowns) == 1
    assert time.monotonic() - started < 8.0
    assert not app.agent_loop.thread.is_alive()
    expected = "закрытие сломано" if failure == "raises" else "0.2"
    assert expected in caplog.text


async def test_second_unmount_is_safe(tmp_path, caplog):
    """Повторный выход не закрывает сервис второй раз и не шумит в лог."""
    import logging

    service = _Service()
    app = _app(tmp_path, _SRV, service)
    app._mcp_consumer = _opened(service)

    with caplog.at_level(logging.WARNING, logger="gc_tui.app"):
        await asyncio.wait_for(app.on_unmount(), _CEILING_SEC)
        await asyncio.wait_for(app.on_unmount(), _CEILING_SEC)

    assert service.closes == 1
    assert "MCP" not in caplog.text


_posix_only = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)


def _real_app(tmp_path, control):
    """Настоящее приложение с настоящим сервисом и stdio-сервером."""
    from gc_core.tools.connect_mcp.service import McpService

    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "mcp.servers": {"srv": stdio_cfg(control)}}
        ),
        mcp_service=McpService(connect_on_start=True),
    )


async def _connected_once(app, pilot) -> None:
    """Дождаться первого итога подключения серверов окна."""

    async def _settled() -> None:
        while not (
            app._mcp_progress is not None
            and app._mcp_progress.state()[1] is not None
        ):
            await pilot.pause(0.05)

    await asyncio.wait_for(_settled(), _CEILING_SEC)
    assert app._mcp_status["srv"]["status"] == STATE_CONNECTED


@_posix_only
async def test_exit_leaves_no_server_process(
    tmp_path, mcp_home, mcp_reaper, mcp_owner
):
    """Настоящее приложение, настоящий сервис и stdio-сервер: после выхода
    процесса сервера нет, поток цикла агента погашен."""
    control = tmp_path / "srv.json"
    app = _real_app(tmp_path, control)
    async with app.run_test(size=(120, 40)) as pilot:
        await _connected_once(app, pilot)
        (pid,) = launches(control)
        assert alive(pid)

    assert app._mcp_service.closed
    await wait_for(lambda: not alive(pid), _CEILING_SEC)
    assert not app.agent_loop.thread.is_alive()


@_posix_only
async def test_exit_with_deaf_server_is_bounded_by_ceiling(
    tmp_path, monkeypatch, mcp_home, mcp_reaper, mcp_owner
):
    """Сервер глух к концу stdin и SIGTERM, потолок закрытия окна меньше
    бюджета сервиса: выход укладывается в потолок и остановку цикла
    (5 с), процесса сервера после выхода нет."""
    from gc_tui import app as app_module

    ceiling = 0.5
    monkeypatch.setattr(app_module, "MCP_CLOSE_TIMEOUT_SEC", ceiling)
    control = tmp_path / "srv.json"
    set_behavior(control, ignore_eof=True, ignore_term=True)
    app = _real_app(tmp_path, control)
    async with app.run_test(size=(120, 40)) as pilot:
        await _connected_once(app, pilot)
        (pid,) = launches(control)
        started = time.monotonic()

    assert time.monotonic() - started < ceiling + _LOOP_SHUTDOWN_SEC + 2
    # Сервер добит аварийным гашением на выходе, а не позже.
    await wait_for(lambda: not alive(pid), 1.0)
    assert not app.agent_loop.thread.is_alive()


# ---- копии статусов окна следуют за сервисом ----


async def test_mcp_command_refreshes_snapshot(tmp_path, monkeypatch):
    """`/mcp` открывается со снимком по текущим статусам сервиса."""
    from gc_core.tools.connect_mcp.registry import mcp_tool_full_name

    from gc_tui.screens.mcp import McpScreen

    _patch_build(monkeypatch)
    service = _Service({"srv": _connected("echo")})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await _build_off_loop(app)
        assert app._mcp_progress.state()[1] is True
        # Тик между сменой статуса и командой не проходит: свежий снимок
        # даёт сама команда.
        service.statuses["srv"] = ServerStatus(
            STATE_CACHED,
            from_catalog=True,
            tools=({"name": "echo"}, {"name": "nap"}),
        )
        app.screen._cmd_mcp()
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, McpScreen)
        info = screen._snapshot["srv"]
        assert info["status"] == STATE_CACHED
        assert {t["name"] for t in info["tools"]} == {
            mcp_tool_full_name("srv", "echo"),
            mcp_tool_full_name("srv", "nap"),
        }


async def test_recovered_server_leaves_notices_without_rebuild(
    tmp_path, monkeypatch
):
    """Сетевой сервер упал на первой попытке (итог шапки — отказ), затем
    подключился повтором: на тике он уходит из уведомления «MCP не
    подключились» и из статусов окна без пересборки агента."""
    _patch_build(monkeypatch)
    failed = ServerStatus(
        STATE_FAILED, reason_code="connect_error", reason="нет связи", retry=1
    )
    service = _Service({"srv": failed})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await _build_off_loop(app)
        assert app._mcp_progress.state()[1] is True
        assert any(n.code == "mcp_failed" for n in app._startup_notices())

        service.statuses["srv"] = _connected("echo")
        app.screen._poll_active()
        await pilot.pause()

        assert app._mcp_status["srv"]["status"] == STATE_CONNECTED
        assert app._mcp_snapshot["srv"]["status"] == STATE_CONNECTED
        assert not any(n.code == "mcp_failed" for n in app._startup_notices())
        assert len(service.opened) == 1


async def test_tick_keeps_snapshot_while_statuses_unchanged(
    tmp_path, monkeypatch
):
    """Статусы не менялись — тик снимок не пересобирает (подписи вызовов
    кэшируются по самому объекту снимка)."""
    _patch_build(monkeypatch)
    service = _Service({"srv": _connected("echo")})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await _build_off_loop(app)
        app.screen._poll_active()
        snapshot = app._mcp_snapshot
        app.screen._poll_active()
        app.screen._poll_active()
        await pilot.pause()

        assert app._mcp_snapshot is snapshot

        service.statuses["srv"] = ServerStatus(STATE_CONNECTING)
        app.screen._poll_active()

        assert app._mcp_snapshot is not snapshot
        assert app._mcp_snapshot["srv"]["status"] == STATE_CONNECTING


async def test_refresh_overlay_failure_keeps_copies(
    tmp_path, monkeypatch, caplog
):
    """Сбой наложения при обновлении копий — предупреждение, экран и тик
    не падают, копии прежние."""
    import logging

    def _broken(*_a, **_k):
        raise TypeError("негодный тул")

    _patch_build(monkeypatch)
    service = _Service({"srv": _connected("echo")})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await _build_off_loop(app)
        snapshot, status = app._mcp_snapshot, app._mcp_status
        monkeypatch.setattr("gc_tui.app.overlay_service_statuses", _broken)
        service.statuses["srv"] = _connected("echo", "nap")
        with caplog.at_level(logging.WARNING, logger="gc_tui.app"):
            app.screen._cmd_mcp()
        await pilot.pause()

        assert app._mcp_snapshot is snapshot
        assert app._mcp_status is status
        assert "не наложены на снимок окна" in caplog.text


async def test_status_read_failure_logged_once(tmp_path, monkeypatch, caplog):
    """Сбой чтения статусов на тике — одно предупреждение на сбой, а не
    на каждый тик; после восстановления новый сбой снова виден."""
    import logging

    _patch_build(monkeypatch)
    service = _Service({"srv": _connected("echo")})
    app = _app(tmp_path, _SRV, service, mounted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await _build_off_loop(app)
        status_of = service.status

        def _broken_status(_consumer):
            raise RuntimeError("статус недоступен")

        def _reads() -> int:
            return caplog.text.count("статусы сервиса не прочитаны")

        with caplog.at_level(logging.WARNING, logger="gc_tui.app"):
            service.status = _broken_status
            for _ in range(3):
                app.refresh_mcp_snapshot()
            assert _reads() == 1
            service.status = status_of
            app.refresh_mcp_snapshot()
            service.status = _broken_status
            app.refresh_mcp_snapshot()
        await pilot.pause()

        assert _reads() == 2


def test_settings_save_without_mcp_reuses_consumer(tmp_path, monkeypatch):
    """Сохранение настроек без смены серверов (`reload_config`) и следующая
    сборка — тот же потребитель, второго открытия нет."""
    _patch_build(monkeypatch)
    cfg_file = tmp_path / "config.yml"
    body = (
        "agent:\n  model: M\n"
        "mcp:\n  servers:\n    srv:\n"
        "      transport: stdio\n      command: srv-mcp\n"
    )
    cfg_file.write_text(body, encoding="utf-8")
    service = _Service({"srv": _connected("echo")})
    app = _app(tmp_path, _SRV, service)
    app._config_path = str(cfg_file)
    app._build_real_executor()
    consumer = app._mcp_consumer

    cfg_file.write_text(
        body + 'backend:\n  completions_url: "http://x/v1"\n',
        encoding="utf-8",
    )
    stamp = time.time() + 5.0
    os.utime(cfg_file, (stamp, stamp))
    app.reload_config()
    app._build_real_executor()

    assert app._mcp_consumer is consumer
    assert len(service.opened) == 1
    assert app._mcp_progress.state()[1] is True


async def test_screen_snapshot_source_unchanged(tmp_path):
    app = _app(tmp_path, {})
    async with app.run_test() as pilot:
        boot = {
            "office": {
                "status": "connected",
                "reason": None,
                "description": "d",
                "tools": [],
            }
        }
        app._mcp_snapshot = boot
        assert pilot.app.screen._mcp_screen_snapshot() is boot


# ---- экран /mcp: четыре состояния ----


def _waiting_snapshot() -> dict:
    return {
        "browser": {
            "status": STATE_CONNECTING,
            "reason": None,
            "description": "",
            "tools": [
                {"name": "mcp_browser_navigate", "description": "Открыть"}
            ],
        },
        "office": {
            "status": STATE_CACHED,
            "reason": None,
            "description": "",
            "tools": [{"name": "mcp_office_read", "description": "Читать"}],
        },
    }


def test_mcp_screen_shows_connecting_and_cached():
    from gc_tui.screens.mcp import McpScreen

    screen = McpScreen(tools_snapshot=_waiting_snapshot())
    for card in screen.cards:
        card.toggle()
    text = mcp_text(screen)

    assert "• browser    подключается · 1 тул" in text
    assert "• office    из кэша · 1 тул" in text
    assert "navigate" in text and "read" in text
    assert "✗" not in text
    summary = text.splitlines()[-1]
    assert summary == (
        "итого: 0 подключены · 1 подключается · 1 из кэша"
        " · 0 не подключены · 2 тула"
    )


def test_mcp_screen_summary_declines_by_number():
    """Итог склоняет состояния и «тулов» по числу; неизвестный статус —
    сырым текстом в строке сервера, в итоге — среди не подключённых."""
    from gc_tui.screens.mcp import McpScreen

    def _server(status: str, n_tools: int) -> dict:
        tools = [{"name": f"t{i}", "description": ""} for i in range(n_tools)]
        return {"status": status, "reason": None, "tools": tools}

    snapshot = {
        "a": _server(STATE_CONNECTED, 3),
        "b": _server(STATE_CONNECTED, 2),
        "c": _server(STATE_CACHED, 0),
        "d": _server(STATE_CACHED, 0),
        "e": _server(STATE_CONNECTING, 0),
        "f": _server(STATE_CONNECTING, 0),
        "g": _server(STATE_FAILED, 0),
        "h": _server("unknown", 0),
    }
    text = mcp_text(McpScreen(tools_snapshot=snapshot))

    assert "✓ a    подключён · 3 тула" in text
    assert "✗ h    unknown" in text
    assert "✗ g    не подключён" in text
    assert text.splitlines()[-1] == (
        "итого: 2 подключены · 2 подключаются · 2 из кэша"
        " · 2 не подключены · 5 тулов"
    )


def _expanded_rows(status: str, tools: list[str]) -> list:
    from gc_tui.screens.mcp import ServerCard

    info = {
        "status": status,
        "reason": None,
        "tools": [{"name": f"mcp_srv_{n}", "description": ""} for n in tools],
    }
    card = ServerCard("srv", info)
    card.toggle()
    return card._renderable().rows


def test_expanded_cached_card_starts_with_note():
    """Раскрытая карточка «из кэша»: первая строка тела — пояснение
    (приглушённо, отступ строк тела), затем тулы."""
    from gc_tui.palette import DIM

    rows = _expanded_rows(STATE_CACHED, ["read"])

    assert rows[1].plain == (
        "    соединения нет · тулы из кэша · подключится при первом вызове"
    )
    assert str(rows[1].style) == DIM
    assert [r.plain.strip() for r in rows[2:]] == ["read"]


@pytest.mark.parametrize(
    ("status", "tools"),
    [
        (STATE_CONNECTED, ["read"]),
        (STATE_CONNECTING, ["read"]),
        (STATE_CONNECTING, []),
    ],
)
def test_expanded_card_without_note(status, tools):
    """У подключённого и подключающегося пояснения нет: тело — тулы."""
    rows = _expanded_rows(status, tools)

    assert [r.plain.strip() for r in rows[1:]] == tools


def test_mcp_screen_waiting_states_use_note_ink():
    from gc_tui.palette import NOTE_INK
    from gc_tui.screens.mcp import McpScreen

    screen = McpScreen(tools_snapshot=_waiting_snapshot())
    head = screen.cards[0]._renderable().rows[0]
    styles = {str(span.style) for span in head.spans}

    assert NOTE_INK in styles
    assert head.plain.startswith("▸ • browser")
