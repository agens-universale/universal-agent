import dataclasses
import functools
import inspect
import logging
import time
from typing import Callable, Optional, TypeVar

_T = TypeVar("_T")


@dataclasses.dataclass(frozen=True)
class _RetryPolicy:
    max_attempts: int
    initial_delay_sec: float
    backoff_factor: float
    should_retry: Optional[Callable[[Exception], bool]]
    retry_log_level: int
    no_more_retries_log_level: int
    operation_name: str

    def retriable(self, attempt: int, exc: Exception) -> bool:
        if attempt >= self.max_attempts:
            return False
        return True if self.should_retry is None else self.should_retry(exc)


def _call_kwargs(kwargs: dict, param_names: set, has_var_keyword: bool, req_logger) -> dict:
    if not has_var_keyword:
        # Отфильтровываем kwargs по параметрам функции, чтобы не словить
        # TypeError: got an unexpected keyword argument ...
        kwargs = {k: v for k, v in kwargs.items() if k in param_names}
    # Re-inject logger в kwargs если функция его принимает (pop выше
    # был чтобы декоратор владел req_logger для своих retry-логов).
    # Без этого вызываемая функция получает logger=None и не пишет
    # детали тела ошибок в файл лога.
    if "logger" in param_names:
        kwargs["logger"] = req_logger
    return kwargs


def _run_with_retries(func, args, kwargs, policy: _RetryPolicy, *, req_logger, url_for_log):
    delay = float(policy.initial_delay_sec)
    last_exc: Optional[Exception] = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not policy.retriable(attempt, exc):
                req_logger.log(
                    policy.no_more_retries_log_level,
                    "%s failed (attempt %s/%s, no more retries). url=%s error=%r",
                    policy.operation_name,
                    attempt,
                    policy.max_attempts,
                    url_for_log,
                    exc,
                )
                raise
            req_logger.log(
                policy.retry_log_level,
                "%s failed (attempt %s/%s). Retrying in %.1f sec url=%s error=%r",
                policy.operation_name,
                attempt,
                policy.max_attempts,
                delay,
                url_for_log,
                exc,
            )
            time.sleep(delay)
            delay *= policy.backoff_factor
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry loop ended unexpectedly")


def retry_with_backoff(
    *,
    max_attempts: int = 5,
    initial_delay_sec: float = 10.0,
    backoff_factor: float = 2.0,
    should_retry: Optional[Callable[[Exception], bool]] = None,
    retry_log_level: int = logging.WARNING,
    no_more_retries_log_level: int = logging.ERROR,
    operation_name: str = "Operation",
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """
    Универсальный декоратор ретраев с экспоненциальным backoff.

    Особенности:
    - управление из kwargs: use_retry (bool), logger (logging.Logger);
    - use_retry извлекается и не пробрасывается в целевую функцию;
    - если should_retry не задан, любая ошибка считается ретраибельной
      до исчерпания max_attempts.
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

    def decorator(func: Callable[..., _T]) -> Callable[..., _T]:
        sig = inspect.signature(func)
        params = sig.parameters
        has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        param_names = set(params.keys())

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> _T:
            use_retry = bool(kwargs.pop("use_retry", False))
            req_logger = kwargs.pop("logger", None) or logging.getLogger(func.__module__)
            url_for_log = kwargs.get("url")
            kwargs = _call_kwargs(kwargs, param_names, has_var_keyword, req_logger)
            if not use_retry or max_attempts <= 1:
                return func(*args, **kwargs)
            return _run_with_retries(func, args, kwargs, policy, req_logger=req_logger, url_for_log=url_for_log)

        return wrapper

    return decorator


__all__ = ["retry_with_backoff"]
