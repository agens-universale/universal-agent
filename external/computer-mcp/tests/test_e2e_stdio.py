import asyncio
import sys

import pytest


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows UIA")
def test_list_tools_via_stdio():
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def _run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "computer_mcp"],
            env=None, cwd=None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                names = {t.name for t in result.tools}
                assert "get_state" in names
                assert "click" in names

    asyncio.run(_run())


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows UIA")
def test_call_get_state_returns_tree():
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def _run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "computer_mcp"],
            env=None, cwd=None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_state", {})
                assert not result.isError
                # content is a list of content blocks; first should be text or structured
                assert len(result.content) >= 1

    asyncio.run(_run())
