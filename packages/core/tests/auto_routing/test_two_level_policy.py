"""Политика `two_level`: решение такта и пара уровней.

Решение — чистая функция. Арифметику шагов проверяет симулятор: он гонит
последовательность тактов через НАСТОЯЩИЕ `state_ops.apply_decision`,
`enter_floor`, `restart_series` и `close_tick` в том порядке, в каком их зовёт
контроллер (решение → вызов → переигрыш либо продление → закрытие такта)."""
from types import SimpleNamespace

import pytest

from gc_core.config_modules.root import AppConfig

from gc_core.auto_routing import state_ops
from gc_core.auto_routing.policy import (
    DEFAULT_STRONG_STEPS, DEFAULT_WEAK_STEPS,
    FINISH_REJECT, LOOP, NO_TOOL_BOUNCE, POLICY_TWO_LEVEL,
    REASON_DESCEND, REASON_HEURISTICS, REASON_HOLD, REASON_PROMOTE,
    REASON_START_TURN, REROUTE_IMPORTANT, TOOL_CALL_ERROR, RoutingParams,
    first_alive, level_below, nearest_levels, pair_levels, two_level_decision,
    with_fallback,
)

#: Продуктовые числа: 10 шагов на слабой, 2 на сильной, пара light+default.
LOW = RoutingParams()
#: Пара default+max.
HIGH = RoutingParams(safe_level="max")
#: Короткие серии — таблицы последовательностей читаются глазами.
SMALL = RoutingParams(weak_steps=4, strong_steps=2)
SMALL_HIGH = RoutingParams(safe_level="max", weak_steps=4, strong_steps=2)


# --- константы и пара ---------------------------------------------------------

def test_constants_are_canon():
    assert (POLICY_TWO_LEVEL, REASON_DESCEND) == ("two_level", "descend")
    assert (DEFAULT_WEAK_STEPS, DEFAULT_STRONG_STEPS) == (10, 2)


def test_routing_params_identify_the_two_level_policy():
    assert RoutingParams().policy == POLICY_TWO_LEVEL
    assert RoutingParams.from_config(AppConfig.model_validate({"agent": {}})).policy == POLICY_TWO_LEVEL
    assert RoutingParams.from_config(AppConfig()).policy == POLICY_TWO_LEVEL


def test_two_level_numbers_default_and_come_from_the_config_subkey():
    assert (RoutingParams().weak_steps, RoutingParams().strong_steps) == (10, 2)
    params = RoutingParams.from_config(AppConfig.model_validate({"agent": {
        "auto_routing_two_level": {"weak_steps": 5, "strong_steps": 2},
    }}))
    assert (params.policy, params.weak_steps, params.strong_steps) == ("two_level", 5, 2)
    partial = RoutingParams.from_config(AppConfig.model_validate({"agent": {"auto_routing_two_level": {"strong_steps": 4}}}))
    assert (partial.weak_steps, partial.strong_steps) == (10, 4)


def test_params_have_only_the_pair_settings():
    from dataclasses import fields

    assert {field.name for field in fields(RoutingParams)} == {
        "safe_level", "weak_steps", "strong_steps", "pair_threshold",
    }


@pytest.mark.parametrize("safe_level, pair", [
    ("default", ("light", "default")),
    ("max", ("default", "max")),
])
def test_pair_is_the_safe_level_and_the_step_below_it(safe_level, pair):
    assert pair_levels(RoutingParams(safe_level=safe_level)) == pair


def test_level_below_is_strict_and_follows_the_level_order():
    assert level_below("light", "default") and level_below("light", "max")
    assert level_below("default", "max")
    assert not level_below("default", "default")
    assert not level_below("max", "default") and not level_below("default", "light")


# --- решение такта: чистая функция ---------------------------------------------

@pytest.mark.parametrize("params, current, step, series, signals, expected", [
    # 1. новый ход — сильная, что бы ни осталось в State
    (LOW, "light", 0, 7, set(), ("default", REASON_START_TURN, (), True)),
    (LOW, "default", 0, 0, set(), ("default", REASON_START_TURN, (), False)),
    (HIGH, "default", 0, 0, set(), ("max", REASON_START_TURN, (), True)),
    # 2. сигналы прошлого такта — сильная, серия с нуля, коды отсортированы
    (LOW, "light", 5, 2, {LOOP}, ("default", REASON_HEURISTICS, (LOOP,), True)),
    (LOW, "default", 5, 2, {TOOL_CALL_ERROR}, ("default", REASON_HEURISTICS, (TOOL_CALL_ERROR,), True)),
    (LOW, "default", 5, 9, {NO_TOOL_BOUNCE, FINISH_REJECT},
     ("default", REASON_HEURISTICS, (FINISH_REJECT, NO_TOOL_BOUNCE), True)),
    (HIGH, "default", 5, 2, {LOOP}, ("max", REASON_HEURISTICS, (LOOP,), True)),
    # 3. вызов шёл не ниже сильной
    (LOW, "default", 1, 1, set(), ("default", REASON_HOLD, (), False)),
    (LOW, "default", 2, 2, set(), ("light", REASON_DESCEND, (), True)),
    (HIGH, "max", 1, 1, set(), ("max", REASON_HOLD, (), False)),
    (HIGH, "max", 2, 2, set(), ("default", REASON_DESCEND, (), True)),
    # 4. вызов шёл ниже сильной
    (LOW, "light", 12, 9, set(), ("light", REASON_HOLD, (), False)),
    (LOW, "light", 13, 10, set(), ("default", REASON_PROMOTE, (), True)),
    (HIGH, "default", 12, 9, set(), ("default", REASON_HOLD, (), False)),
    (HIGH, "default", 13, 10, set(), ("max", REASON_PROMOTE, (), True)),
    # current вне пары: выше сильной (замена мёртвого уровня увела на max)
    (LOW, "max", 1, 1, set(), ("default", REASON_HOLD, (), True)),
    (LOW, "max", 3, 3, set(), ("light", REASON_DESCEND, (), True)),
    # current вне пары: ниже слабой
    (HIGH, "light", 4, 2, set(), ("default", REASON_HOLD, (), True)),
    (HIGH, "light", 13, 10, set(), ("max", REASON_PROMOTE, (), True)),
    # числа — из параметров, не из кода
    (SMALL, "default", 2, 2, set(), ("light", REASON_DESCEND, (), True)),
    (SMALL, "light", 6, 4, set(), ("default", REASON_PROMOTE, (), True)),
])
def test_two_level_decision_table(params, current, step, series, signals, expected):
    decision = two_level_decision(current, step, series, signals, params=params)
    assert (decision.level, decision.reason, decision.heuristics, decision.reset_series) == expected
    assert decision.fallback_from is None


# --- симулятор: счёт шагов на настоящих state_ops --------------------------------

def _simulate(params, script, *, dead=()):
    """Такт за тактом, как контроллер: решение → замена мёртвого уровня →
    `apply_decision` → вызов → (важный тул: ниже сильной — переигрыш `enter_floor`,
    иначе продление `restart_series`) → `close_tick`. Сигнал ставится ПОСЛЕ
    закрытия такта — исполнением батча, и виден решению следующего такта.

    `script` — символ на такт: `.` — тихий, `!` — батч этого такта дал сигнал,
    `I` — в ответе модели важный тул. Итог — строки `уровень вызова:причина`."""
    state = SimpleNamespace()
    state_ops.reset(state)
    state_ops.enter(state)
    _weak, strong = pair_levels(params)
    trace = []
    for tick in script:
        decision = two_level_decision(
            state.current_level, state.steps_in_turn, state.steps_on_current_level,
            state.tick_signals, params=params)
        alive = first_alive(nearest_levels(decision.level), lambda level: level not in dead)
        if alive is not None:
            decision = with_fallback(decision, alive, current=state.current_level)
        state_ops.apply_decision(state, decision)
        call_level = state.current_level
        if tick == "I" and level_below(call_level, strong):
            state_ops.enter_floor(state, strong, REROUTE_IMPORTANT)
            call_level = strong
        elif tick == "I":
            state_ops.restart_series(state)
        state_ops.close_tick(state, call_level)
        if tick == "!":
            state.tick_signals = {TOOL_CALL_ERROR}
        trace.append(f"{call_level}:{state.last_routing_reason}")
    return trace


def test_default_numbers_give_two_strong_calls_then_ten_weak_then_two_strong():
    trace = _simulate(LOW, "." * 15)
    assert [row.split(":")[0] for row in trace] == (
        ["default"] * 2 + ["light"] * 10 + ["default"] * 2 + ["light"])
    assert [row.split(":")[1] for row in trace] == (
        ["start_turn", "hold", "descend"] + ["hold"] * 9
        + ["promote", "hold", "descend"])


@pytest.mark.parametrize("params, script, expected", [
    # старт → спуск → серия слабой до подъёма → снова спуск
    (SMALL, ".........", [
        "default:start_turn", "default:hold",
        "light:descend", "light:hold", "light:hold", "light:hold",
        "default:promote", "default:hold", "light:descend"]),
    # та же арифметика на паре default+max
    (SMALL_HIGH, ".........", [
        "max:start_turn", "max:hold",
        "default:descend", "default:hold", "default:hold", "default:hold",
        "max:promote", "max:hold", "default:descend"]),
    # сигнал от батча слабой: следующий вызов — первый из strong_steps
    (SMALL, "...!....", [
        "default:start_turn", "default:hold", "light:descend", "light:hold",
        "default:heuristics", "default:hold", "light:descend", "light:hold"]),
    # сигнал от батча сильной: серия сильной с нуля, а не спуск по расписанию
    (SMALL, ".!....", [
        "default:start_turn", "default:hold",
        "default:heuristics", "default:hold", "light:descend", "light:hold"]),
    # сигналы подряд держат сильную
    (SMALL, "!!!...", [
        "default:start_turn", "default:heuristics", "default:heuristics",
        "default:heuristics", "default:hold", "light:descend"]),
    # переигрыш слабого вызова: переигранный шаг — первый из strong_steps
    (SMALL, "...I....", [
        "default:start_turn", "default:hold", "light:descend",
        "default:important", "default:hold", "light:descend", "light:hold", "light:hold"]),
    # переигрыш первого же слабого вызова после спуска (серия у close_tick продолжалась бы)
    (SMALL, "..I...", [
        "default:start_turn", "default:hold",
        "default:important", "default:hold", "light:descend", "light:hold"]),
    # продление: важный вызов самой сильной — ещё strong_steps − 1 шагов на сильной
    (SMALL, ".I...", [
        "default:start_turn", "default:hold", "default:hold", "light:descend", "light:hold"]),
    # продление на каждом шаге держит сильную сколько угодно
    (SMALL, "IIII..", [
        "default:start_turn", "default:hold", "default:hold", "default:hold",
        "default:hold", "light:descend"]),
    # важный вызов на самом первом шаге хода серию не удлиняет: она и так 1
    (SMALL, "I...", [
        "default:start_turn", "default:hold", "light:descend", "light:hold"]),
])
def test_step_arithmetic_on_the_real_state_ops(params, script, expected):
    assert _simulate(params, script) == expected


def test_a_dead_strong_level_is_replaced_and_the_series_still_counts():
    """Сильная мертва — её подменяет ближайший живой (max): `current` вне пары, а
    счёт тот же — strong_steps вызовов, спуск, weak_steps вызовов, подъём."""
    assert _simulate(SMALL, ".........", dead=("default",)) == [
        "max:start_turn", "max:hold",
        "light:descend", "light:hold", "light:hold", "light:hold",
        "max:promote", "max:hold", "light:descend"]


def test_a_dead_weak_level_keeps_the_calls_on_the_strong_one():
    trace = _simulate(SMALL, "......", dead=("light",))
    assert [row.split(":")[0] for row in trace] == ["default"] * 6


def test_numbers_from_the_config_drive_the_sequence():
    params = RoutingParams.from_config(AppConfig.model_validate({"agent": {
        "auto_routing_safe_level": "default",
        "auto_routing_two_level": {"weak_steps": 2, "strong_steps": 1},
    }}))
    assert [row.split(":")[0] for row in _simulate(params, "......")] == [
        "default", "light", "light", "default", "light", "light"]


def test_restart_series_zeroes_the_series_and_close_tick_makes_it_one():
    state = SimpleNamespace()
    state_ops.reset(state)
    state_ops.enter(state)
    state_ops.close_tick(state, "default")
    state_ops.close_tick(state, "default")
    assert state.steps_on_current_level == 2
    state.last_routing_reason = "hold"
    state_ops.restart_series(state)
    assert (state.steps_on_current_level, state.current_level, state.last_routing_reason) == (
        0, "default", "hold")
    state_ops.close_tick(state, "default")
    assert (state.steps_on_current_level, state.steps_in_turn) == (1, 3)
