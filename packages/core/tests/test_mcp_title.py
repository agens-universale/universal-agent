from gc_core.tools.connect_mcp.agent_tools import _assemble_mcp_tools_data
from gc_core.tools.connect_mcp.registry import (
    _normalize_tool,
    _registry_entry,
)
from gc_core.tools.mcp_snapshot import build_mcp_tools_snapshot
from gc_testkit.settings import make_settings


def _norm(tool):
    return _normalize_tool(
        "office",
        tool,
        transport="stdio",
        url="",
        server_display_name="Office",
        server_description="",
    )


def test_title_from_tool():
    res = _norm({"name": "read_docx", "title": "Read Word"})
    assert res["_metadata"]["mcp_tool_title"] == "Read Word"


def test_title_from_annotations():
    res = _norm({"name": "t", "annotations": {"title": "A"}})
    assert res["_metadata"]["mcp_tool_title"] == "A"


def test_title_absent():
    assert _norm({"name": "t"})["_metadata"]["mcp_tool_title"] is None


def test_spec_has_no_title():
    res = _norm({"name": "t", "title": "T"})
    assert "title" not in res["_spec"]


def test_snapshot_does_not_mix_prefix_servers():
    settings = make_settings(
        {
            "mcp.servers": {
                "browser": {"transport": "sse", "url": "http://x"},
                "browser_playwright": {
                    "transport": "sse",
                    "url": "http://y",
                },
            }
        }
    )
    tools_data = {
        "mcp_browser_playwright_click": {
            "description": "d",
            "mcp_server": "browser_playwright",
            "mcp_tool": "click",
            "mcp_title": "Click",
        }
    }
    snap = build_mcp_tools_snapshot(settings, tools_data)
    assert snap["browser"]["tools"] == []
    assert snap["browser_playwright"]["tools"] == [
        {
            "name": "mcp_browser_playwright_click",
            "description": "d",
            "tool": "click",
            "title": "Click",
        }
    ]


def test_annotations_not_dict_does_not_break_normalization():
    for bad in (["x"], "title", 5):
        res = _norm({"name": "t", "annotations": bad})
        assert res["_metadata"]["mcp_tool_title"] is None


def test_non_string_title_is_none():
    res = _norm({"name": "t", "title": 5, "annotations": {"title": ["a"]}})
    assert res["_metadata"]["mcp_tool_title"] is None


def test_registry_title_reaches_snapshot_and_not_spec():
    settings = make_settings(
        {"mcp.servers": {"office": {"transport": "sse", "url": "http://x"}}}
    )
    raw = _norm({"name": "read_docx", "title": "Read Word"})
    entry = _registry_entry("office", server_cfg={}, tool=raw)
    registry = {"mcp_office_read_docx": entry}
    tools_data, _ = _assemble_mcp_tools_data(
        registry, {}, settings, {"office": {}}
    )
    data = tools_data["mcp_office_read_docx"]
    assert "title" not in data["spec"]
    snap = build_mcp_tools_snapshot(settings, tools_data)
    assert snap["office"]["tools"][0]["title"] == "Read Word"
