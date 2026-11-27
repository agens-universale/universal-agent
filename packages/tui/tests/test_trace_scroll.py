"""Ближайшая реплика выше верхнего края ленты (спека P3 §2.6, П5)."""

from types import SimpleNamespace

from textual.geometry import Region

from gc_tui.services.trace_scroll import reply_above

REPLIES = {"r1", "r2", "r3"}


def _is_reply(widget):
    return widget in REPLIES


def _at(y, height, widget):
    return SimpleNamespace(region=Region(0, y, 80, height), widget=widget)


# r1 (0–2), строка (3), r2 (5), строка (6–8), r3 (10–11), строка (12)
PLACEMENTS = [
    _at(0, 3, "r1"),
    _at(3, 1, "row-a"),
    _at(5, 1, "r2"),
    _at(6, 3, "row-b"),
    _at(10, 2, "r3"),
    _at(12, 1, "row-c"),
]


def test_no_placements_gives_none():
    assert reply_above([], 10, _is_reply) is None


def test_edge_inside_reply_gives_that_reply():
    assert reply_above(PLACEMENTS, 1, _is_reply) == "r1"
    assert reply_above(PLACEMENTS, 11, _is_reply) == "r3"


def test_reply_exactly_at_top_edge_gives_previous_reply():
    assert reply_above(PLACEMENTS, 5, _is_reply) == "r1"
    assert reply_above(PLACEMENTS, 10, _is_reply) == "r2"


def test_edge_between_replies_gives_nearest_above():
    assert reply_above(PLACEMENTS, 8, _is_reply) == "r2"
    assert reply_above(PLACEMENTS, 13, _is_reply) == "r3"


def test_edge_above_first_reply_gives_none():
    assert reply_above(PLACEMENTS, 0, _is_reply) is None
    rows_first = [_at(0, 2, "row-a"), _at(2, 1, "r1")]
    assert reply_above(rows_first, 2, _is_reply) is None


def test_non_replies_are_skipped():
    seen = []

    def is_reply(widget):
        seen.append(widget)
        return widget in REPLIES

    assert reply_above(PLACEMENTS, 9, is_reply) == "r2"
    assert seen == ["row-b", "r2"]
