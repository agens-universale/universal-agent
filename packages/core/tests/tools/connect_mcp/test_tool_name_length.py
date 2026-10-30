"""Имя MCP-тула для модели не длиннее 64 символов (предел OpenAI-совместимых бэкендов).

Длинное имя ``mcp_<сервер>_<тул>`` укорачивается с хешем; вызов идёт по
метаданным записи (``mcp_server_key`` / ``mcp_tool_name``), а не по имени.
"""

from gc_core.tools.connect_mcp import registry

SERVER = "dodopayments_dodopayments-api"
LONG = "list_subscription_change_history_for_customer_account"


def _normalized(server: str, name: str) -> dict:
    return registry._normalize_tool(
        server,
        {"name": name, "inputSchema": {"type": "object", "properties": {}}},
        transport="stream_http",
        url="https://e.x/mcp",
        server_display_name=server,
        server_description="",
    )


async def test_long_tool_name_is_shortened_and_still_routes(monkeypatch):
    async def fake_discover(items, **_kw):
        return {
            SERVER: (
                [_normalized(SERVER, LONG), _normalized(SERVER, "ping")],
                {"status": "connected"},
            )
        }

    monkeypatch.setattr(registry, "_adiscover_all", fake_discover)
    tools, _ = await registry._abuild_tools_registry(
        {SERVER: {"transport": "stream_http", "url": "https://e.x/mcp"}}
    )

    assert f"mcp_{SERVER}_ping" in tools
    long_keys = [k for k in tools if k != f"mcp_{SERVER}_ping"]
    assert len(long_keys) == 1
    key = long_keys[0]
    assert len(key) <= 64
    assert tools[key]["tool"]["name"] == key
    assert tools[key]["metadata"]["mcp_tool_name"] == LONG
    assert tools[key]["metadata"]["mcp_server_key"] == SERVER


def test_shortened_names_differ_for_different_tools():
    a = registry.mcp_tool_full_name(SERVER, LONG + "_a")
    b = registry.mcp_tool_full_name(SERVER, LONG + "_b")
    assert a != b and len(a) <= 64 and len(b) <= 64
