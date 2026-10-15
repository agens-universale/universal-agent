"""Лестница пар: старт по оценке, подъём, закрепление, спуск (спека pair-ladder §1–§2)."""

from dataclasses import replace

from gc_core.auto_routing.policy import (
    PairState,
    RoutingParams,
    pair_entry_decision,
    pair_start,
    pair_tick,
    turn_params,
)

P = RoutingParams(
    safe_level="auto", pair_threshold=4, strong_steps=2, weak_steps=10
)
LOW = PairState("default", None, 0)


def test_a_score_not_below_the_threshold_keeps_the_upper_pair_all_turn():
    assert pair_start(5, P) == PairState("max", None, 0)
    assert pair_start(4, P) == PairState("max", None, 0)
    assert pair_start(None, P) == PairState(
        "max", None, 0
    )  # сбой судьи — как верхняя
    assert pair_start(3, P) == LOW
    p3 = replace(P, pair_threshold=3)
    assert pair_start(3, p3) == PairState("max", None, 0)
    assert pair_start(2, p3) == LOW


def test_a_signal_after_the_strong_lower_call_raises_for_weak_steps():
    assert pair_tick(LOW, {"loop"}, True, P) == (
        PairState("max", 10, 1),
        "raise",
    )


def test_no_raise_without_the_strong_lower_call_or_without_a_signal():
    assert pair_tick(LOW, {"loop"}, False, P) == (LOW, None)
    assert pair_tick(LOW, set(), True, P) == (LOW, None)


def test_the_second_raise_locks_the_upper_pair():
    assert pair_tick(
        PairState("default", None, 1), {"finish_reject"}, True, P
    ) == (PairState("max", None, 2), "lock")


def test_an_expired_window_descends_and_signals_on_the_upper_pair_change_nothing():
    assert pair_tick(PairState("max", 0, 1), {"loop"}, True, P) == (
        PairState("default", None, 1),
        "descend",
    )
    upper = PairState("max", 3, 0)
    assert pair_tick(upper, {"loop"}, True, P) == (upper, None)


def test_a_locked_pair_never_moves():
    locked = PairState("max", None, 2)
    assert pair_tick(locked, {"loop"}, True, P) == (locked, None)


def test_the_entry_decision_takes_the_strong_of_the_new_pair_with_a_fresh_series():
    down = pair_entry_decision("descend", (), turn_params(P, "default"))
    assert (down.level, down.reason, down.heuristics, down.reset_series) == (
        "default",
        "pair_descend",
        (),
        True,
    )
    up = pair_entry_decision(
        "raise", {"loop", "finish_reject"}, turn_params(P, "max")
    )
    assert (up.level, up.reason, up.heuristics) == (
        "max",
        "pair_raise",
        ("finish_reject", "loop"),
    )
    assert (
        pair_entry_decision("lock", {"loop"}, turn_params(P, "max")).reason
        == "pair_raise"
    )
