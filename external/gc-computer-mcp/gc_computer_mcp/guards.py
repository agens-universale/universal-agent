"""Decorators applied to tools at registration time based on ServerConfig.

Note: in v0.1, only `safe_tool` and `timeout_guard` are wired into
`build_server`. `check_blacklist` is shadowed by an inline check in
`tools.keyboard.shortcut` (kept as a reusable building block for v0.2
when server-level wiring becomes easier). `check_bounds` is unwired —
bounds checking happens inside each tool at the point of coordinate use.
"""

from __future__ import annotations

import functools
import inspect
import logging
import threading
from collections.abc import Callable

from .errors import ComputerMCPError, ErrorCategory

logger = logging.getLogger(__name__)

# Minimal safety net for the most dangerous system combinations.
# Not exhaustive — higher-level policy (agent config, app-specific rules,
# `GC_COMPUTER_MCP_DISABLED_TOOLS`) should handle broader restrictions.
BLACKLISTED_COMBOS: set[frozenset[str]] = {
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"win", "l"}),
    frozenset({"alt", "f4"}),
}


def readonly_guard(*, enabled: bool) -> Callable:
    def decorator(fn: Callable) -> Callable:
        if not enabled:
            return fn

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            raise ComputerMCPError(
                ErrorCategory.READONLY_VIOLATION,
                f"readonly mode active: '{fn.__name__}' is disabled",
            )

        return wrapper

    return decorator


def check_blacklist() -> Callable:
    """Reject dangerous key combinations. Reads `keys` from kwargs or first positional arg."""
    def decorator(fn: Callable) -> Callable:
        try:
            params = list(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            params = []
        has_keys_param = "keys" in params
        keys_pos = params.index("keys") if has_keys_param else -1

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            keys = kwargs.get("keys")
            if keys is None and has_keys_param and 0 <= keys_pos < len(args):
                keys = args[keys_pos]
            if keys is None:
                logger.debug(
                    "check_blacklist applied to %s but no 'keys' argument found; "
                    "decorator is a no-op for this call",
                    fn.__name__,
                )
                return fn(*args, **kwargs)
            norm = frozenset(k.strip().lower() for k in keys)
            if norm in BLACKLISTED_COMBOS:
                raise ComputerMCPError(
                    ErrorCategory.BLACKLISTED_KEYS,
                    f"refusing to execute {'+'.join(sorted(norm))}: "
                    f"system combination is disallowed",
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def check_bounds() -> Callable:
    """Reject negative x/y always; reject out-of-screen only when screen_w/h are passed.

    Caller must pass screen_w/screen_h as kwargs to enable upper-bound check.
    When absent, only negative-coordinate check runs (fail-safe but not exhaustive).
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            x = kwargs.get("x", args[0] if len(args) > 0 else None)
            y = kwargs.get("y", args[1] if len(args) > 1 else None)
            screen_w = kwargs.get("screen_w")
            screen_h = kwargs.get("screen_h")
            if x is not None and y is not None:
                if x < 0 or y < 0:
                    raise ComputerMCPError(
                        ErrorCategory.OUT_OF_BOUNDS,
                        f"coordinates ({x}, {y}) are negative",
                    )
                if screen_w is not None and screen_h is not None:
                    if x >= screen_w or y >= screen_h:
                        raise ComputerMCPError(
                            ErrorCategory.OUT_OF_BOUNDS,
                            f"coordinates ({x}, {y}) outside screen {screen_w}x{screen_h}",
                        )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def timeout_guard(*, seconds: float) -> Callable:
    """Run fn in a worker thread; raise TIMEOUT if not done in time.

    On timeout the underlying thread is NOT interrupted and keeps running —
    logged as a warning. Calling with `seconds` much shorter than worst-case
    fn runtime will leak threads; enforce real limits at the subprocess layer
    where possible.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result: list = [None]
            exc: list = [None]

            def target():
                try:
                    result[0] = fn(*args, **kwargs)
                except BaseException as e:
                    exc[0] = e

            t = threading.Thread(target=target, daemon=True)
            t.start()
            t.join(timeout=seconds)
            if t.is_alive():
                logger.warning(
                    "timeout in %s: thread still alive after %.1fs, "
                    "will continue running in background",
                    fn.__name__, seconds,
                )
                raise ComputerMCPError(
                    ErrorCategory.TIMEOUT,
                    f"'{fn.__name__}' exceeded {seconds:.1f}s",
                )
            if exc[0] is not None:
                raise exc[0]
            return result[0]

        return wrapper

    return decorator


def safe_tool() -> Callable:
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ComputerMCPError:
                raise
            except Exception as e:
                logger.exception("unexpected error in %s", fn.__name__)
                raise ComputerMCPError(
                    ErrorCategory.UNEXPECTED,
                    f"internal error: {type(e).__name__}",
                ) from e

        return wrapper

    return decorator
