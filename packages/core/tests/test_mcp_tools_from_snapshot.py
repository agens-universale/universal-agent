from gc_core.constants.tool_display import (
    mcp_servers_of,
    mcp_tools_from_snapshot,
    tool_display,
)

SNAPSHOT = {
    "fs_srv": {
        "status": "ok",
        "tools": [
            {"name": "mcp_fs_srv_read", "tool": "read", "title": "Читать"},
            {"name": "mcp_fs_srv_ls", "tool": "ls", "title": None},
        ],
    },
    "web": {
        "status": "ok",
        "tools": [{"name": "mcp_web_get", "tool": "get", "title": ""}],
    },
}


def test_wire_names_map_to_server_tool_title():
    tools = mcp_tools_from_snapshot(SNAPSHOT)
    assert tools["mcp_fs_srv_read"] == {
        "server": "fs_srv",
        "tool": "read",
        "title": "Читать",
    }
    assert tools["mcp_fs_srv_ls"]["title"] is None
    assert set(tools) == {"mcp_fs_srv_read", "mcp_fs_srv_ls", "mcp_web_get"}


def test_tool_display_uses_server_and_title_or_tool():
    tools = mcp_tools_from_snapshot(SNAPSHOT)
    shown = tool_display("mcp_fs_srv_read", mcp_tools=tools)
    assert (shown.label, shown.detail) == ("fs_srv", "Читать")
    shown = tool_display("mcp_fs_srv_ls", mcp_tools=tools)
    assert (shown.label, shown.detail) == ("fs_srv", "ls")
    shown = tool_display("mcp_web_get", mcp_tools=tools)
    assert (shown.label, shown.detail) == ("web", "get")


def test_empty_and_broken_snapshots():
    assert mcp_tools_from_snapshot(None) == {}
    assert mcp_tools_from_snapshot({}) == {}
    broken = {
        "a": {"tools": [{"tool": "x"}, {"name": "mcp_a_y"}, "junk", None]},
        "b": None,
        "c": {"tools": None},
        "d": {"tools": [{"name": "mcp_d_z", "tool": "z", "title": 5}]},
    }
    tools = mcp_tools_from_snapshot(broken)
    assert tools == {"mcp_d_z": {"server": "d", "tool": "z", "title": None}}


def test_mcp_servers_of():
    assert mcp_servers_of(SNAPSHOT) == ("fs_srv", "web")
    assert mcp_servers_of(None) == ()
