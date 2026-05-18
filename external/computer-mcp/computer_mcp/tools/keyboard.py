"""Keyboard tools: type_text, key, shortcut."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..core import input as input_mod
from ..core.coordinates import Rect, center_of
from ..core.state_store import StateStore
from ..core.uia import find_by_selector as _default_find_by_selector
from ..errors import ComputerMCPError, ErrorCategory
from ..guards import BLACKLISTED_COMBOS
from .mouse import _resolve_element


def _try_clear_via_value_pattern(live: Any) -> bool:
    """Clear an input-control's value programmatically via UIA ValuePattern.

    More reliable than Ctrl+A/Delete in Electron apps (SberJazz, Teams,
    Slack, VS Code) where keyboard-simulated focus is asynchronous and
    Ctrl+A often lands before React's synthetic focus arrives — the
    shortcut then selects something unrelated (or nothing) and Delete
    removes nothing. UIA ValuePattern.SetValue('') talks to the
    accessibility provider directly, which Electron honours.

    Returns True on success, False on any failure — missing .raw,
    unavailable pattern, or COMError mid-call — so the caller can
    fall back to keyboard simulation.
    """
    raw = getattr(live, "raw", None)
    if raw is None:
        return False
    try:
        pattern = raw.GetValuePattern()
    except Exception:
        return False
    if pattern is None:
        return False
    try:
        pattern.SetValue("")
    except Exception:
        return False
    return True


def type_text(
    *,
    element_id: int,
    text: str,
    clear: bool = False,
    press_enter: bool = False,
    provider: Any,
    store: StateStore,
    _input=input_mod,
    _find_by_selector: Callable = _default_find_by_selector,
) -> dict[str, Any]:
    _, live = _resolve_element(
        element_id=element_id, provider=provider, store=store,
        find_by_selector=_find_by_selector,
    )
    cx, cy = center_of(Rect(*live.bounds))
    _input.click(cx, cy, clicks=1)  # focus
    if clear:
        if not _try_clear_via_value_pattern(live):
            # Fallback: controls without ValuePattern get keyboard simulation.
            _input.hotkey(["ctrl", "a"])
            _input.press_key("delete")
    _input.type_text(text)
    if press_enter:
        _input.press_key("enter")
    return {
        "typed_chars": len(text),
        "state_changed_hint": True,
    }


def key(*, key: str, _input=input_mod) -> dict[str, Any]:
    _input.press_key(key)
    return {"state_changed_hint": True}


def shortcut(*, keys: list[str], _input=input_mod) -> dict[str, Any]:
    norm = frozenset(k.strip().lower() for k in keys)
    if norm in BLACKLISTED_COMBOS:
        raise ComputerMCPError(
            ErrorCategory.BLACKLISTED_KEYS,
            f"refusing to execute {'+'.join(sorted(norm))}: "
            f"system combination is disallowed",
        )
    _input.hotkey([k.lower() for k in keys])
    return {"state_changed_hint": True}
