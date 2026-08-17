def test_build_mcp_tools_snapshot_groups_by_server():
    from gc_core.tools.mcp_snapshot import build_mcp_tools_snapshot
    config = {"mcp-servers": {"search": {"transport": "sse", "url": "http://x"}}}
    # Реальная форма tools_data: ключ тула = mcp_<server>_<base>; значение содержит "description".
    tools_data = {
        "mcp_search_web": {"description": "web search", "spec": {}},
        "read_file": {"description": "builtin", "spec": {}},  # не-MCP — должен быть исключён
    }
    snap = build_mcp_tools_snapshot(config, tools_data)
    assert "search" in snap
    assert any(t["name"] == "mcp_search_web" and "web search" in t["description"]
               for t in snap["search"]["tools"])
    assert all("read_file" not in t["name"] for t in snap["search"]["tools"])  # не-MCP не попал


def test_build_mcp_tools_snapshot_empty_servers():
    from gc_core.tools.mcp_snapshot import build_mcp_tools_snapshot
    assert build_mcp_tools_snapshot({}, {}) == {}
    assert build_mcp_tools_snapshot({"mcp-servers": {}}, {"read_file": {"description": "x"}}) == {}


def test_snapshot_includes_status_and_description():
    from gc_core.tools.mcp_snapshot import build_mcp_tools_snapshot
    config = {"mcp-servers": {
        "office": {"transport": "stdio", "command": "office-mcp", "description": "Read office docs"},
        "computer": {"transport": "stream_stdio", "command": "computer-mcp", "description": "GUI control"},
    }}
    tools_data = {"mcp_office_read": {"description": "read a doc"}}
    mcp_status = {
        "office": {"status": "connected", "reason": None, "tool_count": 1, "elapsed_ms": 5},
        "computer": {"status": "failed", "reason": "команда 'computer-mcp' не найдена",
                     "tool_count": 0, "elapsed_ms": 3},
    }
    snap = build_mcp_tools_snapshot(config, tools_data, mcp_status)
    assert snap["office"]["status"] == "connected"
    assert snap["office"]["description"] == "Read office docs"
    assert snap["office"]["tools"] == [{"name": "mcp_office_read", "description": "read a doc"}]
    assert snap["computer"]["status"] == "failed"
    assert snap["computer"]["reason"] == "команда 'computer-mcp' не найдена"
    assert snap["computer"]["tools"] == []


def test_snapshot_without_status_defaults_unknown():
    from gc_core.tools.mcp_snapshot import build_mcp_tools_snapshot
    config = {"mcp-servers": {"office": {"transport": "stdio", "command": "office-mcp"}}}
    snap = build_mcp_tools_snapshot(config, {}, None)
    assert snap["office"]["status"] == "unknown"
    assert snap["office"]["tools"] == []


def test_update_runtime_meta_mcp_tools_visible_to_filestore(tmp_path):
    # Контракт: ядро пишет runtime.mcp_tools, TUI читает тот же meta-файл.
    from gc_core.thread_store import update_runtime_meta
    from gc_tui.services.stores.file_store import FileTaskStore
    snap = {"search": [{"name": "mcp_search_web", "description": "web"}]}
    update_runtime_meta(str(tmp_path), "cli", "t1", {"mcp_tools": snap})
    store = FileTaskStore(str(tmp_path), system="cli")
    meta = store.get_task_meta("t1")
    assert meta.get("runtime", {}).get("mcp_tools", {}).get("search")[0]["name"] == "mcp_search_web"
