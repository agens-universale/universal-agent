from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from gc_core.utils.json_arg_parser import JsonArgParseError, parse_json_object_arg


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
        @wraps(func)
        def _wrapper(self, *args: Any, **kwargs: Any):
            if source_arg not in kwargs:
                return func(self, *args, **kwargs)

            raw = kwargs.get(source_arg)
            try:
                parsed = parse_json_object_arg(raw, field=source_arg)
            except JsonArgParseError as exc:
                if error_handler_method:
                    handler = getattr(self, error_handler_method)
                    return handler(exc, kwargs)
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
                )

            kwargs[target_arg] = parsed
            kwargs.pop(source_arg, None)
            return func(self, *args, **kwargs)

        return _wrapper

    return _decorator
