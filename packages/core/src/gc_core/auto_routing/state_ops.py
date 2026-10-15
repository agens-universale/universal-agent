from __future__ import annotations

from typing import Any, MutableMapping

from gc_core.auto_routing.policy import (
    START_LEVEL,
    Decision,
    PairState,
)

#: `_last_effective_level` — транзиентное служебное поле для сравнения уровня между
#: тактами (`close_tick`), в State не отображается наружу отдельным контрактом.
#: `_tick_decision` — мемо решения точки решения на текущий такт (кортеж
#: `(live_level, active, decision, effective)`):
#: ретрай `aprepare_main_call` в том же такте (переполнение контекста → force-compaction
#: + `continue`) обязан вернуть то же решение, а не пересчитать его от уже
#: изменённых мутацией входов. `_call_level` — эффективный уровень последнего
#: вызова: `close_tick` читает его через вызывающего вместо `ctx`.
ROUTING_FIELDS = (
    "current_level",
    "steps_in_turn",
    "steps_on_current_level",
    "last_routing_reason",
    "tick_signals",
    "_last_effective_level",
    "_tick_decision",
    "_call_level",
    "turn_safe_level",
    "pair_window_left",
    "pair_raises",
)
#: Поля пары хода (лестница пар): переживают вход в auto, обнуляются границей хода.
_PAIR_FIELDS = ("turn_safe_level", "pair_window_left", "pair_raises")


def _defaults() -> dict[str, Any]:
    return {
        "current_level": None,
        "steps_in_turn": 0,
        "steps_on_current_level": 0,
        "last_routing_reason": None,
        "tick_signals": set(),
        "_last_effective_level": None,
        "_tick_decision": None,
        "_call_level": None,
        "turn_safe_level": None,
        "pair_window_left": None,
        "pair_raises": 0,
    }


def reset(state: Any) -> None:
    """Граница хода: всё в исходное (auto не активен до первого решения)."""
    for name, value in _defaults().items():
        setattr(state, name, value)


def enter(state: Any) -> None:
    """Вход в auto: старт роутинга с default, счётчики с нуля (hold действует).
    Пара хода (`_PAIR_FIELDS`) вход переживает: её выбрал судья этого хода и вела
    лестница, и auto → ручной → auto не зовёт судью второй раз. Обнуляет её только
    граница хода (`reset` из `begin_turn`)."""
    kept = {
        name: getattr(state, name, _defaults()[name]) for name in _PAIR_FIELDS
    }
    reset(state)
    state.current_level = START_LEVEL
    for name, value in kept.items():
        setattr(state, name, value)


def set_pair(state: Any, pair: PairState) -> None:
    """Пара хода от судьи или лестницы: уровень хода, окно верхней пары, подъёмы."""
    state.turn_safe_level = pair.safe_level
    state.pair_window_left = pair.window_left
    state.pair_raises = pair.raises


def read_pair(state: Any) -> PairState:
    """Пара хода из State; пара не выбрана — `PairState(None, None, 0)`."""
    return PairState(
        getattr(state, "turn_safe_level", None),
        getattr(state, "pair_window_left", None),
        int(getattr(state, "pair_raises", 0) or 0),
    )


def leave(state: Any) -> None:
    """Выход из auto: уровень решения снимается, счётчики — факты хода — остаются."""
    state.current_level = None
    state.last_routing_reason = None
    state._tick_decision = None


def apply_decision(state: Any, decision: Decision) -> None:
    """Итоговое решение такта (после замены мёртвого уровня) — в State: уровень
    и причина; серия шагов на уровне обнуляется, если решение просит сброс или
    уровень сменился."""
    level_before = state.current_level
    state.current_level = decision.level
    state.last_routing_reason = decision.reason
    if decision.reset_series or decision.level != level_before:
        state.steps_on_current_level = 0


def enter_floor(state: Any, level: str, reason: str) -> None:
    """Вход на уровень пола переигрышем: уровень и причина пола, серия с нуля
    (работает hold), четвёртый элемент мемо такта — этот уровень: ретрай по
    переполнению контекста в том же такте не уведёт вызов обратно вниз.
    Мемо не позволяет ретраю отменить уже выбранный уровень переигрыша."""
    state.current_level = level
    state.last_routing_reason = reason
    state.steps_on_current_level = 0
    memo = state._tick_decision
    if memo is not None:
        state._tick_decision = (memo[0], memo[1], memo[2], level)


def reroute_tick(state: Any, level: str) -> None:
    """Замена уровня незакрытого такта после подтверждённой недоступности его
    модели (спека PR140 Р3). Как пол: живой выбор и решение такта в мемо
    остаются — смена `/level` во время упавшего запроса действует со следующего
    такта; меняется фактический уровень. Серия с нуля, если уровень сменился
    (то же правило, что у замены мёртвого уровня в решении, `with_fallback`)."""
    if level != state.current_level:
        state.steps_on_current_level = 0
    state.current_level = level
    memo = state._tick_decision
    if memo is not None:
        state._tick_decision = (memo[0], memo[1], memo[2], level)


def restart_series(state: Any) -> None:
    """Серия шагов на текущем уровне — с нуля; уровень и причина прежние. Политика
    `two_level`: важный вызов сделала сама сильная модель, и отсчёт `strong_steps`
    идёт от этого повода. Зовётся ДО `close_tick` — тот сделает из нуля единицу:
    этот шаг первый в новой серии."""
    state.steps_on_current_level = 0


def close_tick(state: Any, level: str) -> None:
    """Успешный LLM-ответ главного цикла (спека §4): счётчики и сигналы такта.
    Такт закрыт — мемо решения (`_tick_decision`) снимается, следующий вызов
    `aroute_before_call` обязан решать заново."""
    state.steps_in_turn = int(getattr(state, "steps_in_turn", 0) or 0) + 1
    previous = getattr(state, "_last_effective_level", None)
    if previous == level:
        state.steps_on_current_level = (
            int(getattr(state, "steps_on_current_level", 0) or 0) + 1
        )
    else:
        state.steps_on_current_level = 1
    state._last_effective_level = level
    state.tick_signals = set()
    # Окно верхней пары тратят только шаги, которые решал auto (в ручном режиме
    # `leave` уже снял уровень до вызова).
    window = getattr(state, "pair_window_left", None)
    if (
        window is not None
        and getattr(state, "current_level", None) is not None
    ):
        state.pair_window_left = max(0, int(window) - 1)
    state._tick_decision = None


def ensure_fields(mapping: MutableMapping[str, Any]) -> None:
    for name, value in _defaults().items():
        mapping.setdefault(name, value)
