from gc_tui.services.mcp_progress import format_mcp_settle


def _srv(ok):
    return {"status": "connected" if ok else "failed"}


def test_all_connected_check():
    s = format_mcp_settle({"a": _srv(True), "b": _srv(True)})
    assert "✓" in s and "все подключены" in s and "/mcp" in s


def test_partial_warning():
    s = format_mcp_settle({"a": _srv(True), "b": _srv(False), "c": _srv(True)})
    assert "⚠" in s and "2/3" in s and "/mcp" in s


def test_none_connected_warning():
    s = format_mcp_settle({"a": _srv(False), "b": _srv(False)})
    assert "⚠" in s and "0/2" in s and "/mcp" in s
