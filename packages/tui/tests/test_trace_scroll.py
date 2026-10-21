"""Ближайшая реплика выше верхнего края ленты (спека P3 §2.6)."""

from gc_tui.services.trace_scroll import reply_above


def test_no_replies_gives_none():
    assert reply_above([], 10) is None


def test_reply_exactly_at_top_edge_is_not_above():
    assert reply_above([(10, "a")], 10) is None


def test_nearest_of_three_above():
    tops = [(0, "a"), (4, "b"), (9, "c"), (30, "d")]
    assert reply_above(tops, 12) == "c"


def test_all_replies_below_top_edge_gives_none():
    assert reply_above([(5, "a"), (8, "b")], 3) is None
