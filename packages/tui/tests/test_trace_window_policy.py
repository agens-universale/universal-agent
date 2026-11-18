"""Политика окна ленты: какие шаги ходов смонтированы (спека шага 2
окна, §5.3, §6). Единица — виджет поддерева ленты, шаг хода —
минимальная единица окна."""

from gc_tui.services import trace_window_policy as policy
from gc_tui.services.trace_window_policy import (
    edge_drops,
    keep_rows,
    near_edge,
    next_chunk,
    prev_chunk,
    short_gap,
    tail_steps,
    trim_to,
    underfilled,
)


def test_tail_covers_budget():
    assert tail_steps([50, 40, 30, 20], 45) == range(2, 4)
    assert tail_steps([50, 40, 30, 20], 50) == range(2, 4)
    assert tail_steps([50, 40, 30, 20], 51) == range(1, 4)
    assert tail_steps([50, 40, 30, 20], 1000) == range(4)
    assert tail_steps([], 100) == range(0)


def test_prev_chunk_whole_turns():
    counts = [10, 20, 30, 40, 50]
    assert prev_chunk(4, counts, 45, 1000) == range(2, 4)
    assert prev_chunk(4, counts, 40, 1000) == range(3, 4)
    assert prev_chunk(2, counts, 1000, 1000) == range(2)
    assert prev_chunk(0, counts, 10, 1000) == range(0)


def test_chunk_not_over_max_unless_one_step():
    """Кусок набирает ``chunk``, но не сверх ``cap``; один шаг — всегда,
    даже больше ``cap``."""
    counts = [100, 80, 30, 70]
    assert prev_chunk(4, counts, 90, 150) == range(2, 4)
    assert prev_chunk(4, counts, 200, 150) == range(2, 4)
    assert prev_chunk(1, counts, 90, 50) == range(1)
    assert next_chunk(0, 4, counts, 90, 150) == range(1)
    assert next_chunk(1, 4, counts, 200, 150) == range(1, 3)
    assert next_chunk(0, 4, counts, 90, 50) == range(1)


def test_chunk_max_holds_chunk_min_and_a_step():
    assert policy.CHUNK_MAX >= policy.CHUNK_MIN


def test_trim_hysteresis():
    assert trim_to(900, 1000, 300) is None
    assert trim_to(1300, 1000, 300) is None
    assert trim_to(1301, 1000, 300) == 301


def test_next_chunk_whole_turns_up_to_gap_end():
    counts = [10, 20, 30, 40, 50]
    assert next_chunk(1, 5, counts, 45, 1000) == range(1, 3)
    assert next_chunk(1, 5, counts, 20, 1000) == range(1, 2)
    assert next_chunk(1, 3, counts, 1000, 1000) == range(1, 3)
    assert next_chunk(3, 3, counts, 10, 1000) == range(3, 3)


def test_edge_drops_whole_turns_from_edge():
    assert edge_drops([100, 200, 300], 150) == 2
    assert edge_drops([100, 200, 300], 100) == 1
    # Снимаемых меньше, чем нужно, — снимаются все снимаемые.
    assert edge_drops([100, 200, 300], 10_000) == 3
    assert edge_drops([], 100) == 0


def test_short_gap_up_to_open_tail():
    assert short_gap(policy.TAIL_MIN) is True
    assert short_gap(policy.TAIL_MIN + 1) is False


def test_keep_rows_margin_only_off_bottom():
    height = 20
    margin = 2 * policy.PREFETCH_SCREENS * height
    assert keep_rows(100, height, stuck=True) == (100, 120)
    assert keep_rows(100, height, stuck=False) == (
        100 - margin,
        120 + margin,
    )


def test_invariant_limit():
    assert policy.WINDOW_LIMIT >= policy.TAIL_MIN + 2 * policy.CHUNK_MIN


def test_giant_turn_is_one_unit():
    assert tail_steps([10, 5000], 600) == range(1, 2)
    assert prev_chunk(2, [10, 5000], 300, 600) == range(1, 2)
    assert edge_drops([5000, 10], 4000) == 1


def test_near_edge_by_screens():
    height = 20
    below = policy.PREFETCH_SCREENS * height
    assert near_edge(below - 1, height) is True
    assert near_edge(below, height) is False


def test_underfilled_by_screens():
    height = 20
    margin = policy.PREFETCH_SCREENS * height
    assert underfilled(margin - 1, height) is True
    assert underfilled(margin, height) is False
