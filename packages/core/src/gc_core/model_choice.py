"""Применённый выбор модели рана и живое событие его смены (спека §5.2).

`State.applied_choice` — выбор (`auto`, уровень или алиас), которым ран
работает сейчас. Старт рана ставит его без события; точка подготовки
основного вызова главного трейса и завершение рана сообщают фронтенду о
смене через хук `RunContext.on_model_choice_applied` и отдают запись смены
для треда (`ModelChoiceRecord`). Сравниваются выборы, а не модели; решения
внутри `auto` событием не считаются.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gc_core.auto_routing.levels import AUTO
from gc_core.persistence.model_choice_record import ModelChoiceRecord


@dataclass(frozen=True)
class ModelChoiceApplied:
    """Выбор применён: `alias` — модель, которой он идёт; `None` — модели
    ещё нет (`auto` на завершении рана). `at_run_end` — применён
    завершением рана, без вызова модели."""

    choice: str
    alias: str | None
    at_run_end: bool = False


def note_applied(
    state: Any, ctx: Any, choice: str, *, at_run_end: bool = False
) -> ModelChoiceRecord | None:
    """Сверить выбор с применённым; сменился — запомнить, сообщить хуку и
    вернуть запись смены для треда (`None` — смены нет).

    Только главный трейс: подготовка вызова субагента событий не даёт.
    Посреди рана модель — цель только что подготовленного вызова."""
    router = getattr(ctx, "model_router", None)
    if router is None or router.is_subagent_child:
        return None
    before = state.applied_choice
    if before == choice:
        return None
    state.applied_choice = choice
    target = choice if at_run_end else state._call_level
    alias = None
    if target != AUTO:
        alias = router.resolve_main_at(target, check_breaker=False).alias
    hook = getattr(ctx, "on_model_choice_applied", None)
    if hook is not None:
        hook(ModelChoiceApplied(choice, alias, at_run_end))
    return ModelChoiceRecord(choice, alias, before)


async def anote_applied(state: Any, ctx: Any, choice: str) -> None:
    """`note_applied` посреди рана и запись смены в тред тут же — за
    последним записанным сообщением (`RunContext.record_model_choice`)."""
    record = note_applied(state, ctx, choice)
    recorder = getattr(ctx, "record_model_choice", None)
    if record is not None and recorder is not None:
        await recorder(record)
