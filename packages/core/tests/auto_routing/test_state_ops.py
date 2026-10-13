from types import SimpleNamespace

from gc_core.auto_routing import state_ops
from gc_core.auto_routing.policy import PairState
from gc_core.auto_routing.state_ops import ROUTING_FIELDS, close_tick, enter, ensure_fields, leave, reset


def _state():
    s = SimpleNamespace()
    reset(s)
    return s


def test_reset_gives_manual_defaults():
    s = _state()
    assert s.current_level is None and s.steps_in_turn == 0 and s.steps_on_current_level == 0
    assert s.last_routing_reason is None
    assert s.tick_signals == set()
    assert s._last_effective_level is None
    assert s._tick_decision is None and s._call_level is None
    assert set(ROUTING_FIELDS) == {"current_level", "steps_in_turn", "steps_on_current_level",
        "last_routing_reason", "tick_signals", "_last_effective_level", "_tick_decision", "_call_level",
        "turn_safe_level", "pair_window_left", "pair_raises"}
    assert not hasattr(s, "last_routing_heuristics")


def test_enter_starts_auto_from_default_with_zero_counters():
    s = _state()
    s.steps_in_turn = 7
    enter(s)
    assert s.current_level == "default" and s.steps_in_turn == 0 and s.steps_on_current_level == 0


def test_leave_clears_current_level_but_keeps_counters():
    s = _state()
    enter(s)
    close_tick(s, "default")
    leave(s)
    assert s.current_level is None and s.steps_in_turn == 1


def test_leave_clears_tick_decision_memo():
    """Выход из auto снимает мемо такта —
    иначе следующий route_before_call мог бы вернуть решение с уже неактуальным
    live_level."""
    s = _state()
    enter(s)
    s._tick_decision = ("auto", True, None, "default")
    leave(s)
    assert s._tick_decision is None


def test_close_tick_increments_counters_and_clears_tick_data():
    s = _state()
    s.tick_signals = {"loop"}
    close_tick(s, "default")
    assert (s.steps_in_turn, s.steps_on_current_level) == (1, 1)
    assert s.tick_signals == set()
    close_tick(s, "default")
    assert s.steps_on_current_level == 2
    close_tick(s, "light")                       # смена эффективного уровня → серия с 1
    assert s.steps_on_current_level == 1


def test_close_tick_clears_tick_decision_memo():
    """Такт закрыт → мемо решения снимается, ретрай СЛЕДУЮЩЕГО
    такта не должен по ошибке унаследовать чужое решение."""
    s = _state()
    s._tick_decision = ("auto", True, None, "default")
    close_tick(s, "default")
    assert s._tick_decision is None


def test_ensure_fields_fills_missing_keys_for_snapshot_load():
    payload = {"steps": 3}
    ensure_fields(payload)
    assert payload["current_level"] is None and payload["tick_signals"] == set()


def test_apply_decision_writes_level_reason_and_resets_the_series_on_change():
    from gc_core.auto_routing.policy import Decision
    from gc_core.auto_routing.state_ops import apply_decision
    s = _state()
    enter(s)
    s.steps_on_current_level = 5
    apply_decision(s, Decision("default", "score", (), False))            # тот же уровень
    assert (s.current_level, s.last_routing_reason, s.steps_on_current_level) == ("default", "score", 5)
    apply_decision(s, Decision("light", "score", (), True))
    assert (s.current_level, s.steps_on_current_level) == ("light", 0)
    s.steps_on_current_level = 3
    apply_decision(s, Decision("default", "health", (), False))           # сброса не просили, уровень сменился
    assert (s.current_level, s.last_routing_reason, s.steps_on_current_level) == ("default", "health", 0)


def test_enter_floor_rewrites_level_reason_series_and_the_memo_level():
    from gc_core.auto_routing.state_ops import enter_floor
    s = _state()
    enter(s)
    s.current_level, s.steps_on_current_level = "light", 4
    s._tick_decision = ("auto", True, None, "light")
    enter_floor(s, "default", "important")
    assert (s.current_level, s.last_routing_reason, s.steps_on_current_level) == ("default", "important", 0)
    assert s._tick_decision == ("auto", True, None, "default")


def test_routing_state_has_no_cascade_or_health_fields():
    from gc_core.auto_routing.state_ops import ROUTING_FIELDS
    removed = {"tests_fail_streak", "stall_streak", "last_test_failures",
               "cascade_fired", "problem_debt", "turn_health"}
    assert removed & set(ROUTING_FIELDS) == set()
    state = SimpleNamespace()
    state_ops.reset(state)
    for name in removed:
        assert not hasattr(state, name), name


def test_close_tick_takes_no_policy_params():
    import inspect
    assert list(inspect.signature(state_ops.close_tick).parameters) == ["state", "level"]


def test_the_turn_boundary_clears_the_pair_and_entering_auto_keeps_it():
    state = SimpleNamespace()
    state_ops.reset(state)
    assert state.turn_safe_level is None
    state_ops.set_pair(state, PairState("default", None, 0))
    state_ops.leave(state)
    state_ops.enter(state)
    assert state.turn_safe_level == "default"
    assert state.current_level == "default" and state.steps_in_turn == 0
    state_ops.reset(state)
    assert state.turn_safe_level is None


def test_the_pair_is_a_routing_field_with_defaults_for_old_snapshots():
    assert "turn_safe_level" in state_ops.ROUTING_FIELDS
    snapshot = {}
    state_ops.ensure_fields(snapshot)                         # снапшот старого формата
    assert snapshot["turn_safe_level"] is None


def test_a_fresh_state_has_no_pair():
    from gc_core.states import State
    s = State(verbose=False, model="m", tools_spec=[], tools_descr=[], skills_list=[], max_steps=5,
              system_prompt_id="v1", use_server_subagents=False)
    assert s.turn_safe_level is None



def test_set_pair_and_read_pair_round_trip_and_entering_auto_keeps_the_pair():
    s = SimpleNamespace()
    reset(s)
    assert state_ops.read_pair(s) == PairState(None, None, 0)
    state_ops.set_pair(s, PairState("max", 8, 1))
    leave(s)
    enter(s)
    assert state_ops.read_pair(s) == PairState("max", 8, 1)
    assert s.current_level == "default" and s.steps_in_turn == 0
    reset(s)
    assert state_ops.read_pair(s) == PairState(None, None, 0)


def test_close_tick_spends_the_window_only_in_auto_and_not_below_zero():
    s = SimpleNamespace()
    reset(s)
    state_ops.set_pair(s, PairState("max", 1, 0))
    s.current_level = None                     # ручной режим: окно не тратится
    close_tick(s, "default")
    assert s.pair_window_left == 1
    s.current_level = "max"
    close_tick(s, "max")
    close_tick(s, "max")
    assert s.pair_window_left == 0


def test_the_pair_fields_are_routing_fields_and_old_snapshots_get_them():
    assert {"turn_safe_level", "pair_window_left", "pair_raises"} <= set(ROUTING_FIELDS)
    snapshot = {}
    ensure_fields(snapshot)
    assert (snapshot["pair_window_left"], snapshot["pair_raises"]) == (None, 0)
