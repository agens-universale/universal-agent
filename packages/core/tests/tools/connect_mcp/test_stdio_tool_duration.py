"""Долгий stdio-тул выполняется один раз и сохраняет persistent-сессию."""

import sys

import pytest
from gc_core.tools.connect_mcp import stream_client as sc


@pytest.mark.parametrize(
    "persistent,warm", [(False, False), (True, False), (True, True)]
)
async def test_slow_stdio_tool_is_not_restarted(
    tmp_path, monkeypatch, persistent, warm
):
    server = tmp_path / "server.py"
    calls = tmp_path / "calls.txt"
    server.write_text(
        "import asyncio, os, sys\n"
        "from pathlib import Path\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "app = FastMCP('duration-test')\n"
        "@app.tool()\n"
        "async def slow(delay: float) -> str:\n"
        "    if delay:\n"
        "        with Path(sys.argv[1]).open('a') as log:\n"
        "            log.write('called\\n')\n"
        "    await asyncio.sleep(delay)\n"
        "    return str(os.getpid())\n"
        "app.run(transport='stdio')\n"
    )
    manager = sc._PersistentSessionManager()
    monkeypatch.setattr(sc, "_SESSION_MANAGER", manager)
    config = {
        "command": sys.executable,
        "args": [str(server), str(calls)],
        "persistent_session": persistent,
    }
    try:
        previous = None
        if warm:
            previous = await sc.acall_tool_stream_stdio(
                config, 0.05, "slow", {"delay": 0}
            )
        result = await sc.acall_tool_stream_stdio(
            config, 0.05, "slow", {"delay": 0.15}
        )
        assert result["isError"] is False
        assert calls.read_text().splitlines() == ["called"]
        if previous is not None:
            assert result["content"] == previous["content"], (
                "процесс/сессия были пересозданы"
            )
    finally:
        # SDK cancel scopes закрываются в той же задаче, что их открыла.
        key = f"stream_stdio::{config['command']}::{','.join(config['args'])}"
        await manager.ainvalidate(key)
