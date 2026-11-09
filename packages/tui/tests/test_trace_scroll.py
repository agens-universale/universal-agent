"""Ближайшая реплика выше верхнего края ленты (спека P3 §2.6)."""

from gc_tui.services.trace_scroll import reply_above

TOPS = {"a": 0, "b": 4, "c": 9, "d": 30}


def _top(name):
    return TOPS[name]


def test_no_replies_gives_none():
    assert reply_above([], 10, _top) is None


def test_reply_exactly_at_top_edge_is_not_above():
    assert reply_above(["c"], 9, _top) is None


def test_nearest_of_three_above():
    assert reply_above(["a", "b", "c", "d"], 12, _top) == "c"


def test_all_replies_below_top_edge_gives_none():
    assert reply_above(["c", "d"], 3, _top) is None


def test_reads_tops_lazily():
    """Верх читается в момент поиска и не у всех реплик (бисекция)."""
    seen = []

    def top(name):
        seen.append(name)
        return TOPS[name]

    assert reply_above(["a", "b", "c", "d"], 12, top) == "c"
    assert len(seen) < 4
