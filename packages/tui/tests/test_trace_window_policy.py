"""Политика окна ленты: какие ходы смонтированы (спека шага 2 окна, §5.3,
§6). Единица — виджет поддерева ленты, ход — минимальная единица окна."""

from gc_tui.services import trace_window_policy as policy
from gc_tui.services.trace_window_policy import (
    drop_count,
    near_top,
    prev_chunk,
    tail_turns,
    trim_to,
)


def test_tail_covers_budget():
    assert tail_turns([50, 40, 30, 20], 45) == range(2, 4)
    assert tail_turns([50, 40, 30, 20], 50) == range(2, 4)
    assert tail_turns([50, 40, 30, 20], 51) == range(1, 4)
    assert tail_turns([50, 40, 30, 20], 1000) == range(0, 4)
    assert tail_turns([], 100) == range(0, 0)


def test_prev_chunk_whole_turns():
    counts = [10, 20, 30, 40, 50]
    assert prev_chunk(4, counts, 45) == range(2, 4)
    assert prev_chunk(4, counts, 40) == range(3, 4)
    assert prev_chunk(2, counts, 1000) == range(0, 2)
    assert prev_chunk(0, counts, 10) == range(0, 0)


def test_trim_hysteresis():
    assert trim_to(900, 1000, 300) is None
    assert trim_to(1300, 1000, 300) is None
    assert trim_to(1301, 1000, 300) == 301


def test_drop_count_keeps_last_turn():
    assert drop_count([100, 200, 300], 150) == 2
    assert drop_count([100, 200, 300], 100) == 1
    assert drop_count([100, 200, 300], 10_000) == 2
    assert drop_count([500], 100) == 0


def test_invariant_limit():
    assert policy.WINDOW_LIMIT >= policy.TAIL_MIN + 2 * policy.CHUNK_MIN


def test_giant_turn_is_one_unit():
    assert tail_turns([10, 5000], 600) == range(1, 2)
    assert prev_chunk(2, [10, 5000], 300) == range(1, 2)
    assert drop_count([5000, 10], 4000) == 1


def test_near_top_by_screens():
    height = 20
    below = policy.PREFETCH_SCREENS * height
    assert near_top(below - 1, height) is True
    assert near_top(below, height) is False
