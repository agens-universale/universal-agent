"""Применённый выбор модели рана и живое событие его смены (спека §5.2).

`State.applied_choice` — выбор (`auto`, уровень или алиас), которым ран
работает сейчас. Старт рана ставит его без события; точка подготовки
основного вызова главного трейса и завершение рана сообщают фронтенду о
смене через хук `RunContext.on_model_choice_applied` и отдают запись смены
для треда (`ModelChoiceRecord`). Сравниваются выборы, а не модели; решения
внутри `auto` событием не считаются. Прежний выбор запись несёт моделью
(`model_of`): по ней фронтенд узнаёт возврат к прежней модели.
"""

from __future__ import annotations

from collections.abc import Callable
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
    state: Any, ctx: Any, choice: str
) -> ModelChoiceRecord | None:
    """Сверить выбор подготовленного вызова с применённым; сменился —
    запомнить, сообщить хуку и вернуть запись смены для треда (`None` —
    смены нет).

    Только главный трейс: подготовка вызова субагента событий не даёт.
    Посреди рана модель — цель только что подготовленного вызова."""
    return _note(state, ctx, lambda _router: choice, at_run_end=False)


def note_applied_at_run_end(state: Any, ctx: Any) -> ModelChoiceRecord | None:
    """`note_applied` на завершении рана: выбор, так и не дошедший до
    вызова, — живой выбор Router'а, его читает сама сверка."""
    return _note(
        state, ctx, lambda router: router.live_choice(), at_run_end=True
    )


def _note(
    state: Any,
    ctx: Any,
    read_choice: Callable[[Any], str],
    at_run_end: bool,
) -> ModelChoiceRecord | None:
    """Сверка выбора под защитой.

    Сбой чтения выбора, резолва модели или хука фронтенда не срывает ни
    вызов посреди рана, ни уборку на его завершении: в лог, записи смены
    нет. Применённый до сбоя выбор остаётся применённым — ран им
    работает; событие этой смены теряется, повтора на следующем такте
    нет."""
    router = getattr(ctx, "model_router", None)
    if router is None or router.is_subagent_child:
        return None
    try:
        choice = read_choice(router)
        before = state.applied_choice
        if before == choice:
            return None
        state.applied_choice = choice
        return _announce(state, ctx, router, before, choice, at_run_end)
    except Exception:
        logger = getattr(ctx, "logger", None)
        if logger is not None:
            logger.warning("model_choice_applied_failed", exc_info=True)
        return None


def _announce(
    state: Any,
    ctx: Any,
    router: Any,
    before: str | None,
    choice: str,
    at_run_end: bool,
) -> ModelChoiceRecord:
    """Собрать запись применённой смены для треда и сообщить о ней хуку:
    событие без записи не уходит."""
    target = choice if at_run_end else state._call_level
    alias = None
    if target != AUTO:
        alias = router.resolve_main_at(target, check_breaker=False).alias
    record = ModelChoiceRecord(choice, alias, model_of(router, before))
    hook = getattr(ctx, "on_model_choice_applied", None)
    if hook is not None:
        hook(ModelChoiceApplied(choice, alias, at_run_end))
    return record


def model_of(router: Any, choice: str | None) -> str | None:
    """Модель выбора для сравнения смен: `auto` — сам выбор, уровень или
    алиас — модель, в которую он резолвится; выбора нет — `None`."""
    if choice is None or choice == AUTO:
        return choice
    resolved = router.resolve_main_at(choice, check_breaker=False)
    return resolved.alias or choice


async def anote_applied(state: Any, ctx: Any, choice: str) -> None:
    """`note_applied` посреди рана и запись смены в тред тут же — за
    последним записанным сообщением (`RunContext.record_model_choice`)."""
    record = note_applied(state, ctx, choice)
    recorder = getattr(ctx, "record_model_choice", None)
    if record is not None and recorder is not None:
        await recorder(record)
