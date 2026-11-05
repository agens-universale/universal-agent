"""Асинхронный двойник ``utils.retry`` (план async-переезда, Задача 7).

Корутинная форма ждёт вызов через ``await``; пауза между попытками
кооперативна и прерывается пользовательским стопом. Всё остальное — извлечение
``use_retry``/``logger`` из kwargs, фильтрация kwargs по сигнатуре, ре-инъекция
логгера, тексты и уровни логов — не повторено, а взято из ``utils.retry``
(``_RetryPolicy``, ``_retry_decorator``): политика ретраев и её следы в логах
не должны различаться от того, каким транспортом ушёл запрос.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional, TypeVar

from gc_core.user_stop import UserStopInterrupt, current_user_stop
from gc_core.utils.retry import _PreparedCall, _retry_decorator, _RetryPolicy

_T = TypeVar("_T")


def _wrap_async(
    func: Callable[..., Awaitable[_T]],
    prepare: Callable[[dict], _PreparedCall],
    policy: _RetryPolicy,
) -> Callable[..., Awaitable[_T]]:
    async def wrapper(*args: Any, **kwargs: Any) -> _T:
        call = prepare(kwargs)
        if not call.use_retry or policy.max_attempts <= 1:
            return await func(*args, **call.kwargs)
        return await _run_with_retries_async(
            func, args, call.kwargs, policy, logger=call.logger, url=call.url
        )

    return wrapper


async def _wait_before_retry(delay: float) -> None:
    stop = current_user_stop()
    if stop is None:
        await asyncio.sleep(delay)
        return
    task = asyncio.current_task()
    cancelled_by_stop = False

    async def release():
        nonlocal cancelled_by_stop
        cancelled_by_stop = task.cancel()

    try:
        async with stop.arm(kind="retry", release=release):
            await asyncio.sleep(delay)
    except UserStopInterrupt:
        # Остановлена пауза после уже начатой попытки, а не новый инструмент.
        raise UserStopInterrupt(kind="retry", started=True) from None
    except asyncio.CancelledError:
        if not cancelled_by_stop:
            raise
        task.uncancel()
        raise UserStopInterrupt(kind="retry", started=True) from None


async def _run_with_retries_async(
    func: Callable[..., Awaitable[_T]],
    args: tuple,
    kwargs: dict,
    policy: _RetryPolicy,
    *,
    logger: Any,
    url: Any,
) -> _T:
    delay = float(policy.initial_delay_sec)
    last_exc: Optional[Exception] = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not policy.retriable(attempt, exc):
                policy.log_no_more_retries(logger, attempt, url, exc)
                raise
            policy.log_retrying(logger, attempt, delay, url, exc)
            await _wait_before_retry(delay)
            delay *= policy.backoff_factor
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry loop ended unexpectedly")


def retry_with_backoff_async(
    *,
    max_attempts: int = 5,
    initial_delay_sec: float = 10.0,
    backoff_factor: float = 2.0,
    should_retry: Optional[Callable[[Exception], bool]] = None,
    retry_log_level: int = logging.WARNING,
    no_more_retries_log_level: int = logging.ERROR,
    operation_name: str = "Operation",
) -> Callable[[Callable[..., Awaitable[_T]]], Callable[..., Awaitable[_T]]]:
    """Ретраи с экспоненциальным backoff для корутинных функций.

    Пауза через ``asyncio.sleep``: ``time.sleep`` заморозил бы весь loop, то
    есть ровно то, ради чего затевался переезд.
    """

    policy = _RetryPolicy(
        max_attempts=max_attempts,
        initial_delay_sec=initial_delay_sec,
        backoff_factor=backoff_factor,
        should_retry=should_retry,
        retry_log_level=retry_log_level,
        no_more_retries_log_level=no_more_retries_log_level,
        operation_name=operation_name,
    )
    return _retry_decorator(policy, _wrap_async)


__all__ = ["retry_with_backoff_async"]
