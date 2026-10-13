"""Двухуровневое исполнение, выбор и переключение пары: чистые правила."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

LEVEL_ORDER = ("light", "default", "max")
POLICY_TWO_LEVEL = "two_level"
DEFAULT_POLICY = POLICY_TWO_LEVEL
START_LEVEL = "default"
DEFAULT_SAFE_LEVEL = "default"
DEFAULT_WEAK_STEPS = 10
DEFAULT_STRONG_STEPS = 2
DEFAULT_PAIR_THRESHOLD = 3
TOOL_CALL_ERROR = "tool_call_error"
LOOP = "loop"
NO_TOOL_BOUNCE = "no_tool_bounce"
FINISH_REJECT = "finish_reject"
REASON_START_TURN = "start_turn"
REASON_HOLD = "hold"
REASON_PROMOTE = "promote"
REASON_HEURISTICS = "heuristics"
REASON_DESCEND = "descend"
REASON_PAIR_RAISE = "pair_raise"
REASON_PAIR_DESCEND = "pair_descend"
REROUTE_IMPORTANT = "important"
REROUTE_INVALID_CALL = "invalid_call"
REROUTE_NO_TOOL_CALL = "no_tool_call"
SAFE_LEVEL_AUTO = "auto"
PAIR_UPPER = "max"
PAIR_LOWER = "default"
PAIR_FALLBACK_LEVEL = PAIR_UPPER
PAIR_SCORE_MIN = 1
PAIR_SCORE_MAX = 5
PAIR_LOCK_AFTER_RAISES = 2
PAIR_START = "start"
PAIR_RAISE = "raise"
PAIR_LOCK = "lock"
PAIR_DESCEND = "descend"


@dataclass(frozen=True)
class RoutingParams:
    safe_level: str = DEFAULT_SAFE_LEVEL
    weak_steps: int = DEFAULT_WEAK_STEPS
    strong_steps: int = DEFAULT_STRONG_STEPS
    pair_threshold: int = DEFAULT_PAIR_THRESHOLD

    @property
    def policy(self) -> str:
        return POLICY_TWO_LEVEL

    @classmethod
    def from_config(cls, source: Any) -> RoutingParams:
        from gc_core.settings import settings_of

        agent = settings_of(source).agent
        two_level = agent.auto_routing_two_level
        return cls(safe_level=agent.auto_routing_safe_level,
                   weak_steps=two_level.weak_steps, strong_steps=two_level.strong_steps,
                   pair_threshold=two_level.pair_threshold)


@dataclass(frozen=True)
class Decision:
    level: str
    reason: str
    heuristics: tuple[str, ...]
    reset_series: bool
    # Уровень, который решение выбрало, но чей алиас разомкнут предохранителем:
    # вместо него взят ближайший живой (`nearest_levels`). None — замены не было.
    fallback_from: str | None = None


def _index(level: str) -> int:
    return LEVEL_ORDER.index(level)


def bump(level: str, delta: int) -> str:
    i = min(max(_index(level) + delta, 0), len(LEVEL_ORDER) - 1)
    return LEVEL_ORDER[i]


def level_below(level: str, other: str) -> bool:
    """`level` строго ниже `other` по порядку уровней (`LEVEL_ORDER`)."""
    return _index(level) < _index(other)


def nearest_levels(level: str) -> tuple[str, ...]:
    """Уровни по близости к `level`: он сам, на ступень выше, на ступень ниже,
    оставшийся. Порядок замены мёртвого уровня живым — в решении автовыбора и в
    цели пола."""
    return tuple(dict.fromkeys((level, bump(level, +1), bump(level, -1)) + LEVEL_ORDER))


def floor_levels(current_level: str, *, params: RoutingParams) -> tuple[str, ...]:
    """Кандидаты цели пола — уровня, на который переигрывается ответ, — для
    вызова уровня `current_level`, по порядку предпочтения: безопасный уровень
    (`params.safe_level`), затем ближайшие к нему (`nearest_levels`) — только
    строго выше `current_level`. Пусто — уровень уже на полу или выше. Функция
    только перечисляет: живость алиасов и сравнение комплектов вызова — у
    вызывающего, которому доступен Router."""
    if _index(current_level) >= _index(params.safe_level):
        return ()
    return tuple(
        level for level in nearest_levels(params.safe_level)
        if _index(level) > _index(current_level)
    )


def first_alive(levels: Iterable[str], is_alive: Callable[[str], bool]) -> str | None:
    """Первый живой уровень из `levels` в их порядке (`nearest_levels` — замена
    мёртвого уровня решения, `floor_levels` — цель пола); живых нет — None.
    Живость — колбэк вызывающего: судить о ней может только Router. Дальше
    первого живого уровня колбэк не спрашивается."""
    return next((level for level in levels if is_alive(level)), None)


def with_fallback(decision: Decision, alive_level: str, *, current: str) -> Decision:
    """Решение, чей уровень мёртв (алиас в предохранителе), с заменой на живой
    `alive_level` (первый живой из `nearest_levels`): причина прежняя,
    `fallback_from` — выбранный уровень; серия сбрасывается, только если живой
    уровень сменил текущий `current`. Замены нет — решение как есть."""
    if alive_level == decision.level:
        return decision
    return replace(decision, level=alive_level, fallback_from=decision.level,
                   reset_series=alive_level != current)


def pair_safe_level(score: int, threshold: int) -> str:
    """Уровень хода по оценке судьи пары: не ниже порога — сложная пара (`max`,
    пара default+max), иначе лёгкая (`default`, пара light+default)."""
    return PAIR_UPPER if score >= threshold else PAIR_LOWER


def turn_params(params: RoutingParams, turn_safe_level: str | None) -> RoutingParams:
    """Параметры такта: `safe_level: auto` заменяется уровнем хода, который выбрал
    судья пары; пары ещё нет — `PAIR_FALLBACK_LEVEL`. Фиксированный `safe_level`
    возвращается как есть."""
    if params.safe_level != SAFE_LEVEL_AUTO:
        return params
    return replace(params, safe_level=turn_safe_level or PAIR_FALLBACK_LEVEL)


@dataclass(frozen=True)
class PairState:
    """Пара хода: уровень хода (сильный уровень пары; `None` — пара ещё не выбрана),
    сколько шагов осталось у верхней пары, поднятой сигналом (`None` — окна нет:
    нижняя или верхняя на весь ход) и сколько подъёмов было в ходу."""
    safe_level: str | None
    window_left: int | None
    raises: int


def pair_start(score: int | None, params: RoutingParams) -> PairState:
    """Старт хода по оценке судьи пары: не ниже порога или сбой судьи (`None`) —
    верхняя пара на весь ход, ниже — нижняя. Стартового окна нет: замер с сильной
    max показал, что после спуска с верхней пары задача теряет её качество."""
    if score is None or pair_safe_level(score, params.pair_threshold) == PAIR_UPPER:
        return PairState(PAIR_UPPER, None, 0)
    return PairState(PAIR_LOWER, None, 0)


def pair_tick(
    pair: PairState, signals: Iterable[str], strong_lower_last: bool, params: RoutingParams,
) -> tuple[PairState, str | None]:
    """Шаг лестницы на такте. Верхняя пара: окно подъёма истекло — спуск (без окна
    не спускается, сигналы её не трогают). Нижняя: сигнал такта после вызова модели
    сильной нижней пары — подъём на `weak_steps` шагов, подъём номер
    `PAIR_LOCK_AFTER_RAISES` — закрепление. Возвращает `(пара, причина смены | None)`."""
    if pair.safe_level == PAIR_UPPER:
        if pair.window_left is not None and pair.window_left <= 0:
            return PairState(PAIR_LOWER, None, pair.raises), PAIR_DESCEND
        return pair, None
    if not (set(signals) and strong_lower_last):
        return pair, None
    raises = pair.raises + 1
    if raises >= PAIR_LOCK_AFTER_RAISES:
        return PairState(PAIR_UPPER, None, raises), PAIR_LOCK
    return PairState(PAIR_UPPER, params.weak_steps, raises), PAIR_RAISE


def pair_entry_decision(cause: str, signals: Iterable[str], params: RoutingParams) -> Decision:
    """Решение такта, на котором сменилась пара: сильная модель новой пары, серия с
    нуля. `params` — уже разрешённые (`turn_params`) под новую пару."""
    _weak, strong = pair_levels(params)
    reason = REASON_PAIR_DESCEND if cause == PAIR_DESCEND else REASON_PAIR_RAISE
    codes = () if cause == PAIR_DESCEND else tuple(sorted(set(signals)))
    return Decision(strong, reason, codes, True)


def pair_levels(params: RoutingParams) -> tuple[str, str]:
    """Пара уровней политики `two_level`: `(слабая, сильная)`. Сильная —
    безопасный уровень (`params.safe_level`), слабая — ступень ниже. На вход
    приходят уже разрешённые параметры (`turn_params`): безопасный уровень —
    `default` или `max`, поэтому ступень ниже есть всегда; третий уровень пары
    не входит."""
    strong = params.safe_level
    return bump(strong, -1), strong


def two_level_decision(
    current: str,
    steps_in_turn: int,
    steps_on_level: int,
    signals: Iterable[str],
    *,
    params: RoutingParams,
) -> Decision:
    """Решение такта под политикой `two_level`, по порядку условий:

    новый ход → сильная (`start_turn`); есть сигналы прошлого такта → сильная,
    серия с нуля (`heuristics` с кодами); вызов шёл не ниже сильной → сильная,
    пока серия короче `strong_steps` (`hold`), иначе слабая (`descend`); вызов
    шёл ниже сильной → слабая, пока серия короче `weak_steps` (`hold`), иначе
    сильная (`promote`).

    «Не ниже / ниже сильной» — по порядку уровней (`level_below`), а не по
    равенству: замена мёртвого уровня живым может увести `current` вне пары, и
    ветка всё равно выбирается верно.

    Счёт серии ведут `state_ops.apply_decision` и `close_tick`: после сброса
    закрытый вызов даёт серию 1, поэтому на сильной проходит ровно `strong_steps`
    вызовов, на слабой — ровно `weak_steps`. Оценку, окно открытия и тул подъёма
    политика не читает."""
    weak, strong = pair_levels(params)
    if steps_in_turn == 0:
        return Decision(strong, REASON_START_TURN, (), strong != current)
    codes = tuple(sorted(set(signals)))
    if codes:
        return Decision(strong, REASON_HEURISTICS, codes, True)
    if not level_below(current, strong):
        if steps_on_level < params.strong_steps:
            return Decision(strong, REASON_HOLD, (), strong != current)
        return Decision(weak, REASON_DESCEND, (), True)
    if steps_on_level >= params.weak_steps:
        return Decision(strong, REASON_PROMOTE, (), True)
    return Decision(weak, REASON_HOLD, (), weak != current)
