"""SIGTERM ``gc-acp`` гасит процессы MCP (спека §5.12).

``serve`` ставит обработчик на сервис фасада, собранного ``build_facade``,
и на выходе — после ``facade.shutdown`` — гасит процессы безусловно, затем
снимает обработчик.
"""

from __future__ import annotations

import asyncio
import io

from gc_acp import main as acp_main
from gc_testkit.mcp.fixtures import mcp_home
from gc_testkit.settings import make_settings

#: Потолок одного ожидания: зависание не повесит прогон.
_CEILING_SEC = 30.0


async def _no_lines():
    """stdin редактора закрыт сразу."""
    return
    yield


async def test_serve_installs_handler(mcp_home, tmp_path, monkeypatch):
    events: list[str] = []
    handled: list = []
    built: list = []
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

    await asyncio.wait_for(acp_main.serve(loaded, io.StringIO()), _CEILING_SEC)

    [facade] = built
    assert facade.mcp_service is not None
    assert handled == [facade.mcp_service]
    assert events == ["handler", "shutdown", "kill", "restore"]
    assert facade.mcp_service.closed
