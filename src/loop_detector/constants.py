"""Thresholds and class decorator. No runtime window — safe to import from tools."""

from __future__ import annotations

from typing import Callable, TypeVar

WINDOW_SIZE = 10
LOOP_WARN_ATTR = "_loop_warn"
LOOP_WARN_KEY = "loop_warn"
READ_PROFILE = {"consecutive": 2, "in_window": 3}
GENERIC_PROFILE = {"consecutive": 3, "in_window": 4}

_T = TypeVar("_T")


def loop_warn_on_repeat(*, consecutive: int, in_window: int) -> Callable[[_T], _T]:
    """Opt-in class decorator: thresholds only, no window memory."""

    def deco(cls: _T) -> _T:
        setattr(cls, LOOP_WARN_ATTR, {"consecutive": consecutive, "in_window": in_window})
        return cls

    return deco
