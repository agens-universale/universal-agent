import pytest

from gc_core.tools.connect_mcp.client_port import AgentRuntimeClient, McpClientPort


def test_port_defines_the_two_operations():
    for name in ("list_tools", "call_tool"):
        assert hasattr(McpClientPort, name)


def test_port_has_no_lifecycle_hook():
    """No close(): session teardown is owned per-operation, not per-client."""
    assert not hasattr(McpClientPort, "close")


async def test_call_tool_does_not_coerce_a_non_dict_answer(monkeypatch):
    """У ответа легаси-формы (не dict) ветка есть — она обязана оставаться живой."""
    from gc_core.tools.connect_mcp import client_port as cp

    async def _fake(**_kw):
        return ["legacy", "list", "answer"]

    monkeypatch.setattr(cp, "acall_tool_stream_http", _fake)
    client = cp.AgentRuntimeClient(
        "srv", {"transport": "stream_http", "url": "http://x:8000/mcp"}
    )
    assert await client.acall_tool("t", {}, timeout=1.0) == ["legacy", "list", "answer"]


async def test_list_tools_routes_stream_http(monkeypatch):
    """Маршрут SDK-транспорта проверяем на КОРУТИННОЙ форме.

    Синхронные `list_tools_stream_*` сняты вместе с маршрутом, который их звал
    (шаги A1–A2): дискавери SDK-транспортов идёт нативно.
    """
    seen = {}

    async def fake_list(*, url, server_cfg, token, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return [{"name": "ask_user"}]

    # conftest подменяет sys.modules["tools"], поэтому строковая цель может
    # разрешиться в ДРУГОЙ объект модуля. Патчим по объекту.
    from gc_core.tools.connect_mcp import client_port as cp

    monkeypatch.setattr(cp, "alist_tools_stream_http", fake_list)
    client = cp.AgentRuntimeClient(
        "srv", {"transport": "stream_http", "url": "http://x:8000/mcp"}
    )

    assert await client.alist_tools(timeout=3.0) == [{"name": "ask_user"}]
    assert seen == {"url": "http://x:8000/mcp", "timeout": 3.0}


async def test_call_tool_routes_stream_http(monkeypatch):
    seen = {}

    async def fake_call(*, url, server_cfg, token, timeout, tool_name, arguments):
        seen["tool"] = tool_name
        return {"ok": True}

    from gc_core.tools.connect_mcp import client_port as cp

    monkeypatch.setattr(cp, "acall_tool_stream_http", fake_call)
    client = cp.AgentRuntimeClient(
        "srv", {"transport": "stream_http", "url": "http://x:8000/mcp"}
    )

    assert await client.acall_tool("echo", {"a": 1}, timeout=2.0) == {"ok": True}
    assert seen == {"tool": "echo"}


def test_sync_port_refuses_sdk_transports_loudly():
    """Синхронный путь для SDK-транспортов обязан отказывать ГРОМКО.

    Попасть туда больше нельзя: дискавери таких серверов идёт нативно. Но
    молчаливый возврат означал бы, что маршрут снова уводит SDK-транспорт в
    поток, — а это и есть дефект, который чинил шаг A1.
    """
    import pytest as _pytest

    from gc_core.tools.connect_mcp import client_port as cp

    client = cp.AgentRuntimeClient(
        "srv", {"transport": "stream_http", "url": "http://x:8000/mcp"}
    )
    with _pytest.raises(RuntimeError, match="асинхронной формой"):
        client.list_tools(timeout=1.0)
    with _pytest.raises(RuntimeError, match="асинхронной формой"):
        client.call_tool("echo", {}, timeout=1.0)


def test_unsupported_transport_raises():
    client = AgentRuntimeClient("srv", {"transport": "carrier-pigeon"})
    with pytest.raises(ValueError):
        client.list_tools(timeout=1.0)


async def test_os_backend_reaches_the_resolver_from_build_tools_data(monkeypatch):
    """The backend RunContext already built must reach the per-server resolver."""
    seen = {}

    class _Sandbox:
        is_sandbox = True

    sandbox = _Sandbox()

    def fake_resolve(server_name, server_cfg, *, os_backend=None):
        seen["backend"] = os_backend
        raise RuntimeError("stop here — we only assert the wiring")

    from gc_core.tools.connect_mcp import registry as reg

    monkeypatch.setitem(
        reg._adiscover_one.__globals__, "resolve_mcp_client", fake_resolve
    )

    cfg = {"transport": "stream_http", "url": "http://x:8000/mcp"}
    tools, status = await reg._adiscover_one("srv", cfg, timeout=1.0, os_backend=sandbox)
    assert tools == []
    assert status["status"] == "failed"
    assert seen["backend"] is sandbox
