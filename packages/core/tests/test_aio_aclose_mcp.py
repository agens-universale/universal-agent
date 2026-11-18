"""`AsyncAgent.aclose` гасит MCP-сессии, поднятые при `create`.

Дискавери в `acreate_run_context` идёт под токеном владельца агента, но гасил
эти сессии только `finally` прогона. Агент, закрытый без прогона, оставлял
долгоживущий stdio (браузер под `@playwright/mcp`) жить до выхода из процесса.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest
from gc_testkit.mcp import alive, exits, launches, stdio_cfg
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from parity_harness import parity_config


async def test_aclose_closes_sessions_owned_by_this_agent(monkeypatch):
    import gc_core.agent_executor as ae
    import gc_core.utils.async_http as ah
    from gc_core.aio.agent import AsyncAgent
    from gc_core.mcp_session_owner import current_owner, reset_owner

    seen: dict = {}

    async def _fake_close(owner_reset, **_kw):
        seen["owner"] = current_owner()
        reset_owner(owner_reset)

    released: list = []

    async def _fake_release():
        released.append(True)

    monkeypatch.setattr(ae, "_aclose_mcp_and_release", _fake_close)
    monkeypatch.setattr(ah, "release_clients", _fake_release)

    ctx = SimpleNamespace(mcp_session_owner="agent-7", logger=None)
    agent = AsyncAgent(SimpleNamespace(_ctx=ctx), set())
    await agent.aclose()

    assert seen.get("owner") == "agent-7", (
        "сессии закрывались не под токеном агента"
    )
    assert released == [True], "пул не отпущен"
    assert current_owner() != "agent-7", "владение не снято после закрытия"

    await agent.aclose()  # идемпотентно
    assert released == [True]


@pytest.mark.skipif(os.name != "posix", reason="процессы и сигналы POSIX")
async def test_aclose_keeps_sessions_of_mcp_consumer(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """С потребителем сервиса MCP сессии контекста — его: агент их не
    закрывает, закрывает тот, кто открыл потребителя."""
    from gc_core.aio.agent import AsyncAgent
    from gc_core.tools.connect_mcp.service import McpService

    control = tmp_path / "srv.json"
    settings = parity_config(
        tmp_path,
        monkeypatch,
        **{
            "mcp.servers": {"srv": stdio_cfg(control)},
            "mcp.meta.discovery_timeout_sec": 30,
        },
    )
    service = McpService(connect_on_start=True)
    consumer = mcp_reaper.track(service.open_consumer("агент"))
    agent = await asyncio.wait_for(
        AsyncAgent.create(settings, mcp_consumer=consumer), 30
    )
    (pid,) = launches(control)

    await asyncio.wait_for(agent.aclose(), 30)

    assert exits(control) == []
    assert alive(pid)
