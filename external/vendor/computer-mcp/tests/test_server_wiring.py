
from computer_mcp.config import ServerConfig
from computer_mcp.server import build_server

ALL_BASE = {
    "get_state", "wait", "click", "type", "scroll", "drag",
    "key", "shortcut", "resize", "switch", "launch_app",
}
VISION = {"capture_screenshot", "analyze_screen_region"}


def _tool_names(mcp):
    """Extract registered tool names from FastMCP.

    Uses the async list_tools() API (stable in mcp 1.26.0) so the test
    doesn't depend on internal attribute layout.
    """
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    return {t.name for t in tools}


def test_defaults_register_only_base_tools():
    mcp = build_server(ServerConfig())
    names = _tool_names(mcp)
    assert names == ALL_BASE


def test_vision_enabled_registers_vision_tools():
    mcp = build_server(ServerConfig(enable_vision=True))
    names = _tool_names(mcp)
    assert names == ALL_BASE | VISION


def test_readonly_keeps_only_readonly_tools():
    mcp = build_server(ServerConfig(readonly=True))
    names = _tool_names(mcp)
    assert names == {"get_state", "wait"}


def test_readonly_with_vision():
    mcp = build_server(ServerConfig(readonly=True, enable_vision=True))
    names = _tool_names(mcp)
    assert names == {"get_state", "wait", "capture_screenshot", "analyze_screen_region"}


def test_disabled_tools_subtracts_from_registered():
    mcp = build_server(ServerConfig(disabled_tools=frozenset({"drag", "shortcut"})))
    names = _tool_names(mcp)
    assert "drag" not in names
    assert "shortcut" not in names
    assert "click" in names
