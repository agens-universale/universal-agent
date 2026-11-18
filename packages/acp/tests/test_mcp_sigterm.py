"""SIGTERM ``gc-acp`` гасит процессы MCP (спека §5.12).

``serve`` ставит обработчик на сервис фасада, собранного ``build_facade``,
и на выходе — после ``facade.shutdown`` — гасит процессы безусловно, затем
снимает обработчик.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from gc_acp import main as acp_main
from gc_testkit.mcp.fixtures import mcp_home
from gc_testkit.settings import make_settings

#: Потолок одного ожидания: зависание не повесит прогон.
_CEILING_SEC = 30.0


async def _no_lines():
    """stdin редактора закрыт сразу."""
    return
    yield


def _wire(monkeypatch, tmp_path, events, *, kill_interrupts=False):
    """``serve`` на настоящем ``build_facade`` с записью событий: остановка
    фасада, гашение, установка и снятие обработчика. ``kill_interrupts`` —
    гашение прерывается ``KeyboardInterrupt``. Отдаёт настройки, собранные
    фасады и сервисы, на которые ставился обработчик."""
    built: list = []
    handled: list = []
    real_build = acp_main.build_facade

    def _build(loaded, protocol_out):
        facade = real_build(loaded, protocol_out)
        service = facade.mcp_service
        real_shutdown, real_kill = facade.shutdown, service.kill_processes_now

        async def shutdown() -> None:
            events.append("shutdown")
            await real_shutdown()

        def kill_processes_now() -> None:
            events.append("kill")
            real_kill()
            if kill_interrupts:
                raise KeyboardInterrupt

        facade.shutdown = shutdown
        service.kill_processes_now = kill_processes_now
        built.append(facade)
        return facade

    def _install(service):
        events.append("handler")
        handled.append(service)
        return lambda: events.append("restore")

    monkeypatch.setattr(acp_main, "build_facade", _build)
    monkeypatch.setattr(acp_main, "kill_mcp_on_sigterm", _install)
    monkeypatch.setattr(acp_main, "stdin_lines", _no_lines)
    loaded = make_settings(
        {"paths.threads_data_path": str(tmp_path), "os_backend": "local"}
    )
    return loaded, built, handled


async def test_serve_installs_handler(mcp_home, tmp_path, monkeypatch):
    events: list[str] = []
    loaded, built, handled = _wire(monkeypatch, tmp_path, events)

    await asyncio.wait_for(acp_main.serve(loaded, io.StringIO()), _CEILING_SEC)

    [facade] = built
    assert facade.mcp_service is not None
    assert handled == [facade.mcp_service]
    assert events == ["handler", "shutdown", "kill", "restore"]
    assert facade.mcp_service.closed


async def test_interrupted_kill_still_restores_handler(
    mcp_home, tmp_path, monkeypatch
):
    """Ctrl+C во время гашения на выходе: обработчик SIGTERM всё равно
    снимается."""
    events: list[str] = []
    loaded, built, _ = _wire(
        monkeypatch, tmp_path, events, kill_interrupts=True
    )

    # `asyncio.timeout`, а не `wait_for`: на Python 3.11 `wait_for` уводит
    # корутину в задачу, и `KeyboardInterrupt` из неё рушит цикл теста.
    with pytest.raises(KeyboardInterrupt):
        async with asyncio.timeout(_CEILING_SEC):
            await acp_main.serve(loaded, io.StringIO())

    [facade] = built
    assert events == ["handler", "shutdown", "kill", "restore"]
    assert facade.mcp_service.closed
