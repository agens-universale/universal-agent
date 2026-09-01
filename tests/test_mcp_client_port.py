import pytest

from tools.connect_mcp.client_port import AgentRuntimeClient, McpClientPort


def test_port_defines_the_three_operations():
    for name in ("list_tools", "call_tool", "close"):
        assert hasattr(McpClientPort, name)


def test_close_is_a_noop_by_default():
    client = AgentRuntimeClient("srv", {"transport": "http", "url": "http://x/mcp"})
    assert client.close() is None


def test_list_tools_routes_stream_http(monkeypatch):
    seen = {}

    def fake_list(*, url, server_cfg, token, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return [{"name": "ask_user"}]

    # conftest replaces sys.modules["tools"], so a string target can resolve to a
    # different module object than the class was imported from. Use one object.
    import tools.connect_mcp.client_port as cp

    monkeypatch.setattr(cp, "list_tools_stream_http", fake_list)
    client = cp.AgentRuntimeClient(
        "srv", {"transport": "stream_http", "url": "http://x:8000/mcp"}
    )
    assert client.list_tools(timeout=5.0) == [{"name": "ask_user"}]
    assert seen == {"url": "http://x:8000/mcp", "timeout": 5.0}


def test_call_tool_routes_stream_http(monkeypatch):
    def fake_call(*, url, server_cfg, token, timeout, tool_name, arguments):
        return {"result": {"content": [{"type": "text", "text": tool_name}]}}

    import tools.connect_mcp.client_port as cp

    monkeypatch.setattr(cp, "call_tool_stream_http", fake_call)
    client = cp.AgentRuntimeClient(
        "srv", {"transport": "stream_http", "url": "http://x:8000/mcp"}
    )
    out = client.call_tool("ask_user", {"question": "q"}, timeout=5.0)
    assert out["result"]["content"][0]["text"] == "ask_user"


def test_unsupported_transport_raises():
    client = AgentRuntimeClient("srv", {"transport": "carrier-pigeon"})
    with pytest.raises(ValueError):
        client.list_tools(timeout=1.0)
