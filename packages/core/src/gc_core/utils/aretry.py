"""Асинхронный двойник ``utils.retry`` (план async-переезда, Задача 7).

Отличий от синхронного ровно три: ``async def`` у обёртки, ``await`` у вызова
и ``asyncio.sleep`` вместо ``time.sleep``. Всё остальное — извлечение
``use_retry``/``logger`` из kwargs, фильтрация kwargs по сигнатуре, ре-инъекция
логгера, тексты и уровни логов — повторено дословно: политика ретраев и её
следы в логах не должны различаться от того, каким транспортом ушёл запрос.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True)
class _RetryPolicy:
    max_attempts: int
    initial_delay_sec: float
    backoff_factor: float
    should_retry: Optional[Callable[[Exception], bool]]
    retry_log_level: int
    no_more_retries_log_level: int
    operation_name: str

    def _retriable(self, attempt: int, exc: Exception) -> bool:
        if attempt >= self.max_attempts:
            return False
        return True if self.should_retry is None else self.should_retry(exc)

    async def arun(self, func: Callable[..., Awaitable[_T]], args: tuple, kwargs: dict, *, logger: Any, url: Any) -> _T:
        delay = float(self.initial_delay_sec)
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if not self._retriable(attempt, exc):
                    logger.log(
                        self.no_more_retries_log_level,
                        "%s failed (attempt %s/%s, no more retries). "
                        "url=%s error=%r",
                        self.operation_name, attempt, self.max_attempts, url, exc,
                    )
                    raise
                logger.log(
                    self.retry_log_level,
                    "%s failed (attempt %s/%s). Retrying in %.1f sec "
                    "url=%s error=%r",
                    self.operation_name, attempt, self.max_attempts, delay, url, exc,
                )
                await asyncio.sleep(delay)
                delay *= self.backoff_factor
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("retry loop ended unexpectedly")


def _prepare_kwargs(kwargs: dict, has_var_keyword: bool, param_names: set, req_logger: Any) -> dict:
    if not has_var_keyword:
        kwargs = {k: v for k, v in kwargs.items() if k in param_names}
    if "logger" in param_names:
        kwargs["logger"] = req_logger
    return kwargs


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

    def decorator(func: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
        sig = inspect.signature(func)
        params = sig.parameters
        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        param_names = set(params.keys())

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> _T:
            use_retry = bool(kwargs.pop("use_retry", False))
            req_logger = kwargs.pop("logger", None) or logging.getLogger(func.__module__)
            url_for_log = kwargs.get("url")
            kwargs = _prepare_kwargs(kwargs, has_var_keyword, param_names, req_logger)
            if not use_retry or max_attempts <= 1:
                return await func(*args, **kwargs)
            return await policy.arun(func, args, kwargs, logger=req_logger, url=url_for_log)

        return wrapper

    return decorator


__all__ = ["retry_with_backoff_async"]
