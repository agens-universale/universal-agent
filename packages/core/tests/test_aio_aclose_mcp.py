"""`AsyncAgent.aclose` гасит MCP-сессии, поднятые при `create`.

Дискавери в `acreate_run_context` идёт под токеном владельца агента, но гасил
эти сессии только `finally` прогона. Агент, закрытый без прогона, оставлял
долгоживущий stdio (браузер под `@playwright/mcp`) жить до выхода из процесса.
"""

from types import SimpleNamespace


async def test_aclose_closes_sessions_owned_by_this_agent(monkeypatch):
    import gc_core.agent_executor as ae
    import gc_core.utils.async_http as ah
    from gc_core.aio.agent import AsyncAgent
    from gc_core.mcp_session_owner import current_owner, reset_owner

    seen: dict = {}

    async def _fake_close(owner_reset):
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

    assert seen.get("owner") == "agent-7", "сессии закрывались не под токеном агента"
    assert released == [True], "пул не отпущен"
    assert current_owner() != "agent-7", "владение не снято после закрытия"

    await agent.aclose()  # идемпотентно
    assert released == [True]
