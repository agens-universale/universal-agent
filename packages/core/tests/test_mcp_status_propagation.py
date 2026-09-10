from gc_core.tools.connect_mcp import registry as reg
from gc_core import tools as tools_pkg


async def test_get_mcp_tools_data_returns_status(monkeypatch):
    def fake_build(servers_cfg, *, discovery_timeout_sec=8, on_progress=None,
                   os_backend=None):
        return ({}, {"office": {"status": "failed", "reason": "команда 'office-mcp' не найдена",
                                "tool_count": 0, "elapsed_ms": 3}})
    monkeypatch.setattr(reg, "_build_tools_registry", fake_build)
    from gc_core.tools.connect_mcp.agent_tools import aget_mcp_tools_data_for_config
    tools_data, status = await aget_mcp_tools_data_for_config(
        {"mcp-servers": {"office": {"transport": "stdio", "command": "office-mcp"}}}
    )
    assert status["office"]["status"] == "failed"


async def test_build_tools_data_returns_mcp_status(monkeypatch):
    async def fake_mcp(config_dict, on_progress=None, **_kwargs):
        return ({}, {"x": {"status": "connected", "reason": None, "tool_count": 0, "elapsed_ms": 1}})
    # Подменяем НАСТОЯЩИЙ шов дискавери. Реэкспорт `get_mcp_tools_data` в
    # `gc_core.tools` был тонкой синхронной обёрткой и снят вместе с ней;
    # прод и раньше звал функцию из `agent_tools` напрямую.
    from gc_core.tools.connect_mcp import agent_tools as _at

    monkeypatch.setattr(_at, "aget_mcp_tools_data_for_config", fake_mcp)
    result = await tools_pkg.abuild_tools_data({"mcp-servers": {"x": {"transport": "stdio", "command": "x"}}})
    assert len(result) == 4
    assert result[3]["x"]["status"] == "connected"
