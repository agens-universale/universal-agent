"""Применённый выбор модели рана и живое событие его смены (спека §5.2).

`State.applied_choice` — выбор (`auto`, уровень или алиас), которым ран
работает сейчас. Старт рана ставит его без события; точка подготовки
основного вызова главного трейса и завершение рана сообщают фронтенду о
смене через хук `RunContext.on_model_choice_applied` и отдают запись смены
для треда (`ModelChoiceRecord`). Запись подготовки ждёт запроса
(`State.pending_choice_record`): до него цикл дописывает в тред отложенный
вывод тула, и смена встаёт за ним. Сравниваются выборы, а не модели; решения
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


def note_applied_at_run_end(state: Any, ctx: Any) -> ModelChoiceRecord | None:
    """Сверка (`note_applied`) на завершении рана: выбор, так и не
    дошедший до вызова, — живой выбор Router'а, его читает сама сверка.
    Запись смены возвращается — её пишет исполнитель после итога хода."""
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


def note_applied(state: Any, ctx: Any, choice: str) -> None:
    """Сверить выбор подготовленного вызова с применённым; сменился —
    запомнить и сообщить хуку сразу, а запись смены отложить до запроса
    (`awrite_pending`).

    Только главный трейс: подготовка вызова субагента событий не даёт.
    Посреди рана модель — цель только что подготовленного вызова."""
    record = _note(state, ctx, lambda _router: choice, at_run_end=False)
    if record is not None:
        state.pending_choice_record = record


def take_pending(state: Any) -> ModelChoiceRecord | None:
    """Отложенная запись смены — один раз: её пишет запрос или, если стоп
    пришёл до запроса, завершение рана."""
    record = state.pending_choice_record
    state.pending_choice_record = None
    return record


async def awrite_pending(state: Any, ctx: Any) -> None:
    """Записать отложенную смену в тред за последним записанным
    сообщением (`RunContext.record_model_choice`) — перед запросом."""
    record = take_pending(state)
    recorder = getattr(ctx, "record_model_choice", None)
    if record is not None and recorder is not None:
        await recorder(record)
