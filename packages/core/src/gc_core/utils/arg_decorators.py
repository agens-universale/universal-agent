from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Callable

from gc_core.utils.json_arg_parser import (
    JsonArgParseError,
    parse_json_object_arg,
)


def _parse_or_reject(
    self,
    kwargs: dict,
    source_arg: str,
    target_arg: str,
    error_handler_method: str | None,
):
    """``(ошибка, kwargs)``: разобрать аргумент либо отдать отказ тула."""
    if source_arg not in kwargs:
        return None, kwargs
    raw = kwargs.get(source_arg)
    try:
        parsed = parse_json_object_arg(raw, field=source_arg)
    except JsonArgParseError as exc:
        if error_handler_method:
            handler = getattr(self, error_handler_method)
            return handler(exc, kwargs), kwargs
        return self._result(  # type: ignore[attr-defined]
            ok=False,
            status="rejected",
            message="Validation failed.",
            task_report="",
            error={
                "type": "invalid_argument",
                "field": exc.field,
                "message": exc.message,
            },
        ), kwargs
    kwargs[target_arg] = parsed
    kwargs.pop(source_arg, None)
    return None, kwargs


def parse_json_object_argument(
    *,
    source_arg: str,
    target_arg: str,
    error_handler_method: str | None = None,
) -> Callable:
    """
    Parse JSON-object string argument and inject parsed dict.

    If parsing fails, decorator returns tool-style validation response via self._result(...)
    with status=rejected/message=Validation failed.
    """

    def _decorator(func: Callable) -> Callable:
        def _parsed_or_error(self, kwargs: dict):
            return _parse_or_reject(
                self, kwargs, source_arg, target_arg, error_handler_method
            )

        # Корутинную функцию оборачиваем корутиной. Синхронная обёртка вернула
        # бы отказ ГОТОВЫМ СЛОВАРЁМ, минуя func, и вызывающий с ``await`` упал
        # бы на нём с TypeError вместо аккуратного отказа. Тот же приём, что в
        # ``security/dual_wrapper.dual_wrap``.
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def _awrapper(self, *args: Any, **kwargs: Any):
                early, kwargs = _parsed_or_error(self, kwargs)
                if early is not None:
                    return early
                return await func(self, *args, **kwargs)

            return _awrapper

        @wraps(func)
        def _wrapper(self, *args: Any, **kwargs: Any):
            early, kwargs = _parsed_or_error(self, kwargs)
            if early is not None:
                return early
            return func(self, *args, **kwargs)

        return _wrapper

    return _decorator
