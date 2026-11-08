from gc_tui.services.mcp_progress import McpProgress


def test_sink_begin_update_snapshot():
    p = McpProgress()
    p.begin(["a", "b"])
    servers, active = p.snapshot()
    assert active and servers == {"a": None, "b": None}
    p.update("a", {"status": "connected"})
    p.update("b", {"status": "failed", "reason": "x"})
    servers, _ = p.snapshot()
    assert servers["a"]["status"] == "connected"


def test_finish_records_outcome():
    sink = McpProgress()
    assert sink.outcome() is None
    sink.finish(True)
    assert sink.outcome() is True
