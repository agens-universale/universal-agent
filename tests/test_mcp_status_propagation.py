import tools.connect_mcp.registry as reg
import tools as tools_pkg


def test_get_mcp_tools_data_returns_status(monkeypatch):
    def fake_build(servers_cfg, *, discovery_timeout_sec=8, on_progress=None):
        return ({}, {"office": {"status": "failed", "reason": "команда 'office-mcp' не найдена",
                                "tool_count": 0, "elapsed_ms": 3}})
    monkeypatch.setattr(reg, "_build_tools_registry", fake_build)
    from tools.connect_mcp.agent_tools import get_mcp_tools_data_for_config
    tools_data, status = get_mcp_tools_data_for_config(
        {"mcp-servers": {"office": {"transport": "stdio", "command": "office-mcp"}}}
    )
    assert status["office"]["status"] == "failed"


def test_build_tools_data_returns_mcp_status(monkeypatch):
    def fake_mcp(config_dict, on_progress=None):
        return ({}, {"x": {"status": "connected", "reason": None, "tool_count": 0, "elapsed_ms": 1}})
    monkeypatch.setattr(tools_pkg, "get_mcp_tools_data", fake_mcp)
    result = tools_pkg.build_tools_data({"mcp-servers": {"x": {"transport": "stdio", "command": "x"}}})
    assert len(result) == 4
    assert result[3]["x"]["status"] == "connected"
