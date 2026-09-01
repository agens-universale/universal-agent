import pytest

from tools.connect_mcp.client_port import AgentRuntimeClient, McpClientPort


def test_port_defines_the_two_operations():
    for name in ("list_tools", "call_tool"):
        assert hasattr(McpClientPort, name)


def test_port_has_no_lifecycle_hook():
    """No close(): session teardown is owned per-operation, not per-client."""
    assert not hasattr(McpClientPort, "close")


def test_call_tool_does_not_coerce_a_non_dict_answer(monkeypatch):
    """call_mcp_tool has a branch for legacy non-dict answers; keep it reachable."""
    import tools.connect_mcp.client_port as cp

    monkeypatch.setattr(
        cp, "call_tool_stream_http",
        lambda **kw: ["legacy", "list", "answer"],
    )
    client = cp.AgentRuntimeClient(
        "srv", {"transport": "stream_http", "url": "http://x:8000/mcp"}
    )
    assert client.call_tool("t", {}, timeout=1.0) == ["legacy", "list", "answer"]


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


def test_os_backend_reaches_the_resolver_from_build_tools_data(monkeypatch):
    """The backend RunContext already built must reach the per-server resolver."""
    seen = {}

    class _Sandbox:
        is_sandbox = True

    sandbox = _Sandbox()

    def fake_resolve(server_name, server_cfg, *, os_backend=None):
        seen["backend"] = os_backend
        raise RuntimeError("stop here — we only assert the wiring")

    from tools.connect_mcp import registry as reg

    monkeypatch.setitem(
        reg._discover_one.__globals__, "resolve_mcp_client", fake_resolve
    )

    cfg = {"transport": "stream_http", "url": "http://x:8000/mcp"}
    tools, status = reg._discover_one("srv", cfg, timeout=1.0, os_backend=sandbox)
    assert tools == []
    assert status["status"] == "failed"
    assert seen["backend"] is sandbox
