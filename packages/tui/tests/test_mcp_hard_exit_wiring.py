"""Аварийное гашение процессов MCP в TUI (спека §5.12).

Уборка сторожа терминала (SIGHUP, мёртвый tty) зовёт
``kill_processes_now`` первым шагом. Выход приложения при сбое или сроке
закрытия сервиса гасит процессы сразу, а после остановки цикла агента —
всегда.
"""

from __future__ import annotations

import asyncio

import pytest
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
_SRV = {"mcp.servers": {"srv": {"transport": "stdio", "command": "srv-mcp"}}}


class _Service:
    """Двойник сервиса: порядок закрытия и гашения."""

    def __init__(self, events: list, on_close=None, **_kwargs) -> None:
        self.events = events
        self.on_close = on_close
        self.closed = False

    async def aclose(self, budget: float = 10.0) -> None:
        self.closed = True
        self.events.append("close")
        if self.on_close is not None:
            await self.on_close()

    def kill_processes_now(self) -> None:
        self.closed = True
        self.events.append("kill")


def _app(tmp_path, service, *, opened: bool = True):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M", **_SRV}),
        mcp_service=service,
    )
    if opened:
        app._mcp_consumer = object()
    return app


def _record_shutdown(app, events: list) -> None:
    real = app.agent_loop.shutdown

    def shutdown(timeout: float = 5.0) -> None:
        events.append("loop_shutdown")
        real(timeout)

    app.agent_loop.shutdown = shutdown


def _cfg_for_main(tmp_path):
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
    return cfg_path


def test_sighup_cleanup_kills_mcp(monkeypatch, tmp_path):
    """Уборка сторожа (терминал умер посреди `app.run()`) гасит процессы
    MCP сервиса окна первым шагом — раньше git пикера, демонов и голоса."""
    from gc_tui import app as tui_app
    from gc_tui.services import file_index, terminal_guard
    from gc_tui.voice_runtime import autostart

    calls: list[str] = []
    services: list[_Service] = []
    guards: list = []

    def make_service(**kwargs) -> _Service:
        service = _Service(calls, **kwargs)
        services.append(service)
        return service

    class _SpyGuard(terminal_guard.TerminalGuard):
        def __init__(self, cleanup, **kw):
            super().__init__(cleanup, **kw)
            guards.append(self)

    class _FakeApp:
        def __init__(self, **kw):
            self.mcp_service = kw["mcp_service"]

        def set_config_path(self, path):
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):
            pass

        def run(self):
            assert self.mcp_service is services[0]
            guards[0].run_cleanup()  # аварийный путь сторожа

    monkeypatch.setattr(
        "gc_core.tools.connect_mcp.service.McpService", make_service
    )
    monkeypatch.setattr(terminal_guard, "TerminalGuard", _SpyGuard)
    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons",
        lambda cfg, **kw: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons",
        lambda: calls.append("mcp"),
    )
    monkeypatch.setattr(
        autostart, "release_voice_service", lambda: calls.append("voice")
    )
    monkeypatch.setattr(
        file_index, "kill_running_processes", lambda: calls.append("git")
    )

    from gc_tui import main

    assert main(argv=["--config", str(_cfg_for_main(tmp_path))]) == 0
    assert calls == ["ensure", "kill", "git", "mcp", "voice"]


@pytest.mark.parametrize("failure", ["raises", "hangs"])
async def test_unmount_falls_back_to_kill(tmp_path, monkeypatch, failure):
    """`aclose` бросает или не укладывается в потолок: гашение — сразу,
    до остановки цикла агента, и ещё раз после неё."""
    from gc_tui import app as app_module

    monkeypatch.setattr(app_module, "MCP_CLOSE_TIMEOUT_SEC", 0.2)

    async def _raise():
        raise RuntimeError("закрытие сломано")

    async def _hang():
        await asyncio.sleep(60)

    events: list[str] = []
    on_close = _raise if failure == "raises" else _hang
    app = _app(tmp_path, _Service(events, on_close))
    _record_shutdown(app, events)

    await asyncio.wait_for(app.on_unmount(), _CEILING_SEC)

    assert events == ["close", "kill", "loop_shutdown", "kill"]


@pytest.mark.parametrize("opened", [True, False])
async def test_unmount_kills_after_loop_shutdown(tmp_path, opened):
    """Штатный выход: после остановки цикла агента — безусловное гашение
    (потребителя могли открыть уже после проверки на выходе)."""
    events: list[str] = []
    app = _app(tmp_path, _Service(events), opened=opened)
    _record_shutdown(app, events)

    await asyncio.wait_for(app.on_unmount(), _CEILING_SEC)

    expected = ["close"] if opened else []
    assert events == [*expected, "loop_shutdown", "kill"]
