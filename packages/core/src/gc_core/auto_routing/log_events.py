"""События лога автовыбора — `routing_decision` и `artifact_reroute` (приложение B
спеки). Презентер: собирает поля события из State и решения и пишет одну запись
уровня info. Логгер отдаёт вызывающий (`ctx.logger` или фолбэк контроллера) —
модуль ни ctx, ни State не пишет. Импортирует его только контроллер."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from gc_core.auto_routing.policy import Decision, RoutingParams


def log_routing_decision(
    logger: Any,
    state: Any,
    *,
    active: bool,
    decision: Optional[Decision],
    level: str,
    resolved: Optional[Any],
    params: RoutingParams,
) -> None:
    """Событие `routing_decision`: `mode`/`reason` по режиму, `score`/`heuristics`
    — только при активном auto, счётчики — всегда.

    Поле уровня называется `routing_level`, НЕ `level`: `JsonlFormatter.format`
    (`utils/logging_module.py`) сначала
    кладёт `payload["level"] = record.levelname` (severity записи, `INFO`), а
    цикл по `extra` пропускает ключи, уже занятые в `payload`, — одноимённое
    поле `extra["level"]` молча терялось на реальном форматтере.

    Сообщение записи человекочитаемое (`level=… reason=… score=…
    model=…`) — в текстовом формате лога голое `"routing_decision"` не несло
    ничего, кроме имени события; `event` для jsonl остаётся в `extra`
    (`JsonlFormatter` берёт готовый `record.getMessage()` как есть, отдельно от
    остальных полей `payload`)."""
    model = None
    if resolved is not None:
        model = resolved.alias or resolved.model
    if model is None:
        model = getattr(state, "default_model", None)
    # Причина — из State: `state_ops.apply_decision` пишет туда причину итогового
    # решения такта, она же в `decision.reason`; решение — запасной источник.
    reason = getattr(state, "last_routing_reason", None) if active else "manual"
    if reason is None:
        reason = decision.reason if decision is not None else "manual"
    payload: Dict[str, Any] = {
        "event": "routing_decision",
        "mode": "auto" if active else "manual",
        "routing_level": level,
        "model": model,
        "reason": reason,
        "steps_in_turn": getattr(state, "steps_in_turn", 0),
        "steps_on_current_level": getattr(state, "steps_on_current_level", 0),
    }
    if active and decision is not None:
        payload["score"] = None
        payload["heuristics"] = list(decision.heuristics)
        # Политика — не режим: `mode` остаётся auto/manual, случайное решение
        # узнаётся по reason.
        payload["policy"] = params.policy
        if decision.fallback_from is not None:
            # Выбранный уровень мёртв (предохранитель) — вызов идёт ближайшим живым.
            payload["fallback_from"] = decision.fallback_from
    logger.info(
        "routing_decision level=%s reason=%s score=%s model=%s",
        level, reason, payload.get("score"), model,
        extra=payload,
    )


def log_routing_pair_judge(
    logger: Any,
    *,
    turn: int,
    mid_turn: bool,
    choice: Any,
    pair_threshold: int,
    pair: Tuple[str, str],
    card_chars: int,
    latency_ms: int,
) -> None:
    """Событие `routing_pair_judge`: один вызов судьи пары. `source: fallback` с
    `error` — судья не дал оценки, ход идёт на сложной паре. `mid_turn` — судью
    звали после переключения на auto посреди хода."""
    payload: Dict[str, Any] = {
        "event": "routing_pair_judge",
        "turn": turn,
        "mid_turn": mid_turn,
        "score": choice.score,
        "reason": choice.reason,
        "pair_threshold": pair_threshold,
        "safe_level": choice.safe_level,
        "pair": list(pair),
        "source": choice.source,
        "card_chars": card_chars,
        "latency_ms": latency_ms,
    }
    if choice.error:
        payload["error"] = choice.error
    logger.info(
        "routing_pair_judge turn=%s score=%s safe_level=%s source=%s",
        turn, choice.score, choice.safe_level, choice.source, extra=payload,
    )


def log_routing_pair(
    logger: Any,
    *,
    turn: int,
    step: int,
    cause: str,
    before: Optional[Any],
    after: Any,
    score: Optional[int] = None,
    signals: Tuple[str, ...] = (),
) -> None:
    """Событие `routing_pair`: смена пары хода (лестница пар). `step` — шаги с входа в
    auto; `from` — уровень хода до смены (на старте — `None`)."""
    payload: Dict[str, Any] = {
        "event": "routing_pair",
        "turn": turn,
        "step": step,
        "cause": cause,
        "from": before.safe_level if before is not None else None,
        "to": after.safe_level,
        "window_left": after.window_left,
        "raises": after.raises,
    }
    if cause == "start":
        payload["score"] = score
    if signals:
        payload["signals"] = list(signals)
    logger.info("routing_pair turn=%s step=%s cause=%s to=%s", turn, step, cause, after.safe_level,
                extra=payload)


def log_artifact_reroute(
    logger: Any,
    state: Any,
    *,
    from_level: str,
    to_level: str,
    tools: Tuple[str, ...],
    triggers: Tuple[str, ...],
    usage_before: Optional[Any],
    discarded_calls: List[Dict[str, str]],
    discarded_text: str,
) -> None:
    """Событие `artifact_reroute` (имя историческое — его читают разборщики
    стенда): ответ вызова ниже пола дал повод переигрыша — отброшен, тот же запрос
    ушёл повторно на уровне пола. `triggers` — коды поводов (`policy.REROUTE_*`),
    `tools` — важные тулы ответа (у повода без тула список пуст). `usage` и `cost`
    — расход ИМЕННО отброшенного вызова (снимок `usage_before` до него + дельта).
    `discarded_calls` / `discarded_text` — сам отброшенный ответ (все вызовы с
    аргументами как есть и его текст, `response.discarded_answer`): материал
    сравнения с ответом пола; пустые не пишутся."""
    payload: Dict[str, Any] = {
        "event": "artifact_reroute",
        "triggers": list(triggers),
        "from_level": from_level,
        "to_level": to_level,
        "tools": list(tools),
    }
    if discarded_calls:
        payload["discarded_calls"] = discarded_calls
    if discarded_text:
        payload["discarded_text"] = discarded_text
    if usage_before is not None and getattr(state, "usage", None) is not None:
        delta = state.usage.delta_from(usage_before).totals()
        payload["prompt_tokens"] = delta.get("input")
        payload["completion_tokens"] = delta.get("output")
        cost_by_currency = delta.get("cost_by_currency") or {}
        if cost_by_currency:
            payload["cost"] = cost_by_currency
    logger.info(
        "artifact_reroute from_level=%s to_level=%s triggers=%s tools=%s",
        from_level, to_level, list(triggers), list(tools),
        extra=payload,
    )
