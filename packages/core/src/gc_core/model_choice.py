"""Применённый выбор модели рана и живое событие его смены (спека §5.2).

`State.applied_choice` — выбор (`auto`, уровень или алиас), которым ран
работает сейчас. Старт рана ставит его без события; точка подготовки
основного вызова главного трейса и завершение рана сообщают фронтенду о
смене через хук `RunContext.on_model_choice_applied`. Сравниваются выборы, а
не модели; решения внутри `auto` событием не считаются.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gc_core.auto_routing.levels import AUTO


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
) -> None:
    """Сверить выбор с применённым; сменился — записать и сообщить хуку.

    Только главный трейс: подготовка вызова субагента событий не даёт.
    Посреди рана модель — цель только что подготовленного вызова."""
    router = getattr(ctx, "model_router", None)
    if router is None or router.is_subagent_child:
        return
    if state.applied_choice == choice:
        return
    state.applied_choice = choice
    hook = getattr(ctx, "on_model_choice_applied", None)
    if hook is None:
        return
    target = choice if at_run_end else state._call_level
    alias = None
    if target != AUTO:
        alias = router.resolve_main_at(target, check_breaker=False).alias
    hook(ModelChoiceApplied(choice, alias, at_run_end))
