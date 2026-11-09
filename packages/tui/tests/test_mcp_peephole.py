"""Модель «глазка» MCP: очередь показа, поколения, итог по тикам."""

from __future__ import annotations

from gc_tui.services.mcp_peephole import McpPeephole

OK = {"status": "connected"}
BAD = {"status": "failed"}


def _run(p: McpPeephole, n: int) -> None:
    for _ in range(n):
        p.tick()


def _hold_of_first(servers: dict) -> int:
    p = McpPeephole()
    p.sync(object(), servers, None)
    p.tick()
    first = p.shown
    n = 1
    while p.shown == first:
        p.tick()
        n += 1
    return n - 1


def test_zero_servers_is_settled_before_first_snapshot():
    p = McpPeephole(0)
    assert p.phase == "settled"
    assert p.outcome is None  # итог виден, а сборка ещё не кончилась


def test_n_servers_is_loading_with_total():
    p = McpPeephole(3)
    assert (p.phase, p.total) == ("loading", 3)


def test_outcome_reports_end_of_build():
    p = McpPeephole(0)
    p.sync(object(), {}, True)
    assert (p.phase, p.outcome) == ("settled", True)


def test_shows_each_server_for_five_ticks_when_alone():
    p = McpPeephole()
    src = object()
    p.sync(src, {"a": OK, "b": None}, None)
    _run(p, 5)
    assert p.shown == ("a", True)
    p.sync(src, {"a": OK, "b": OK}, None)
    p.tick()
    assert p.shown == ("b", True)


def test_burst_shortens_hold():
    assert _hold_of_first({f"s{i}": OK for i in range(20)}) == 1
    assert _hold_of_first({f"s{i}": OK for i in range(8)}) == 2
    assert _hold_of_first({f"s{i}": OK for i in range(3)}) == 5


def test_counter_counts_connected_only():
    p = McpPeephole()
    p.sync(object(), {"a": OK, "b": BAD}, True)
    _run(p, 6)
    assert (p.connected, p.total) == (1, 2)
    q = McpPeephole()
    q.sync(object(), {"a": BAD}, True)
    _run(q, 15)
    assert (q.phase, q.connected, q.total) == ("settled", 0, 1)


def test_settles_ten_ticks_after_last_hold():
    p = McpPeephole()
    p.sync(object(), {"a": OK}, True)
    _run(p, 14)
    assert p.phase == "loading"
    p.tick()
    assert p.phase == "settled"
    assert p.shown is None


def test_waits_for_outcome_before_settle():
    p = McpPeephole()
    src = object()
    p.sync(src, {"a": OK}, None)
    _run(p, 40)
    assert p.phase == "loading"
    p.sync(src, {"a": OK}, True)
    _run(p, 9)
    assert p.phase == "loading"
    p.tick()
    assert p.phase == "settled"


def test_finish_ok_marks_unreported_failed():
    p = McpPeephole()
    p.sync(object(), {"a": None, "b": None}, True)
    _run(p, 40)
    assert (p.phase, p.connected, p.total) == ("settled", 0, 2)


def test_finish_error_fails():
    p = McpPeephole()
    src = object()
    p.sync(src, {"a": OK}, None)
    p.tick()
    p.sync(src, {"a": OK}, False)
    assert p.phase == "failed"
    assert p.shown is None
    _run(p, 30)
    assert p.phase == "failed"


def test_new_source_resets_and_drops_pending_settle():
    p = McpPeephole()
    a, b = object(), object()
    p.sync(a, {"x": OK}, True)
    gen = p.generation
    _run(p, 10)
    p.sync(b, {"y": None}, None)
    assert p.generation == gen + 1
    assert p.connected == 0
    _run(p, 30)
    assert p.phase == "loading"


def test_new_source_with_zero_servers_settles_without_mcp():
    p = McpPeephole()
    p.sync(object(), {"a": OK}, None)
    p.sync(object(), {}, None)
    assert (p.phase, p.total) == ("settled", 0)
