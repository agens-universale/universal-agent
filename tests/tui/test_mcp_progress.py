from tui.services.mcp_progress import McpProgress, format_mcp_progress, format_mcp_settle


def test_sink_begin_update_snapshot():
    p = McpProgress()
    p.begin(["a", "b"])
    servers, active = p.snapshot()
    assert active and servers == {"a": None, "b": None}
    assert not p.all_reported()
    p.update("a", {"status": "connected"})
    p.update("b", {"status": "failed", "reason": "x"})
    assert p.all_reported()
    servers, _ = p.snapshot()
    assert servers["a"]["status"] == "connected"


def test_format_progress_multiline_block():
    text = format_mcp_progress({"a": {"status": "connected"}, "b": None,
                                "c": {"status": "failed"}})
    lines = text.splitlines()
    assert lines[0].startswith("подключаю MCP")          # заголовок
    assert len(lines) == 4                                # заголовок + 3 сервера
    body = lines[1:]
    assert any("a" in ln and "✓" in ln for ln in body)   # каждый сервер на своей строке
    assert any("b" in ln and "⏳" in ln for ln in body)
    assert any("c" in ln and "✗" in ln for ln in body)


def test_format_settle_all_ok():
    text = format_mcp_settle({"a": {"status": "connected"}})
    assert text.splitlines() == ["✓ MCP: все подключены", "✓ готов к работе"]


def test_format_settle_failed():
    text = format_mcp_settle({"a": {"status": "connected"}, "office": {"status": "failed"}})
    status, ready = text.splitlines()
    assert "office" in status and "/mcp" in status
    assert "(F3)" in status   # подсказка хоткея экрана /mcp
    assert ready == "✓ готов к работе"


def test_format_settle_singular_vs_plural():
    one = format_mcp_settle({"office": {"status": "failed"}})
    assert "недоступен" in one and "недоступны" not in one
    two = format_mcp_settle({"office": {"status": "failed"}, "computer": {"status": "failed"}})
    assert "недоступны" in two
