from gc_tui.services.mcp_progress import McpProgress


def test_sink_begin_update_snapshot():
    p = McpProgress()
    p.begin(["a", "b"])
    servers, outcome = p.state()
    assert servers == {"a": None, "b": None} and outcome is None
    p.update("a", {"status": "connected"})
    p.update("b", {"status": "failed", "reason": "x"})
    servers, _ = p.state()
    assert servers["a"]["status"] == "connected"


def test_finish_records_outcome():
    sink = McpProgress()
    assert sink.outcome() is None
    sink.finish(True)
    assert sink.outcome() is True


def test_state_is_one_consistent_snapshot():
    """Статусы и итог — одним снимком под замком: при `True` в нём уже все
    статусы, порядок вызовов у читателя не важен."""
    p = McpProgress()
    p.begin(["a"])
    p.update("a", {"status": "connected"})
    p.finish(True)
    assert p.state() == ({"a": {"status": "connected"}}, True)
