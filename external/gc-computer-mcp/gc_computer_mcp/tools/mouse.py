"""Mouse tools: click, scroll, drag."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..core import input as input_mod
from ..core.coordinates import Rect, center_of
from ..core.state_store import StateExpiredError, StateNotFoundError, StateStore
from ..core.uia import find_by_selector as _default_find_by_selector
from ..core.uia_types import snapshot_from_dict
from ..errors import ComputerMCPError, ErrorCategory


def _resolve_element(
    *,
    element_id: int,
    provider: Any,
    store: StateStore,
    find_by_selector: Callable = _default_find_by_selector,
):
    try:
        raw = store.load()
    except StateNotFoundError as e:
        raise ComputerMCPError(
            ErrorCategory.STALE_STATE,
            "no recent state snapshot, call get_state first",
        ) from e
    except StateExpiredError as e:
        raise ComputerMCPError(
            ErrorCategory.STALE_STATE,
            f"state snapshot expired: {e}. Call get_state first.",
        ) from e

    snap = snapshot_from_dict(raw)
    el = snap.by_id(element_id)
    if el is None:
        raise ComputerMCPError(
            ErrorCategory.ELEMENT_NOT_FOUND,
            f"element_id {element_id} not in last snapshot. Call get_state again.",
        )

    scope_hwnd = raw.get("scope_hwnd")

    # Tier 1: scoped fast path (runtime_id under scope_hwnd if set, else desktop).
    live = provider.find_by_runtime_id(el.runtime_id, scope_hwnd=scope_hwnd)
    if live is not None:
        return el, live

    # Tier 2: scoped slow path (selector DFS under scope_hwnd if set).
    live = find_by_selector(
        provider=provider,
        name=el.name,
        control_type=el.control_type,
        automation_id=el.automation_id,
        parent_path=el.parent_path,
        scope_hwnd=scope_hwnd,
    )
    if live is not None:
        return el, live

    # Tier 3: desktop fallback (only if we were scoped — window may have been
    # destroyed/recreated; the element might exist under a different top-level).
    if scope_hwnd is not None:
        live = provider.find_by_runtime_id(el.runtime_id, scope_hwnd=None)
        if live is not None:
            return el, live
        live = find_by_selector(
            provider=provider,
            name=el.name,
            control_type=el.control_type,
            automation_id=el.automation_id,
            parent_path=el.parent_path,
            scope_hwnd=None,
        )
        if live is not None:
            return el, live

    raise ComputerMCPError(
        ErrorCategory.ELEMENT_NOT_FOUND,
        f"element_id {element_id} ({el.control_type} \"{el.name}\") "
        f"not found in current UI. Call get_state again.",
    )


def click(
    *,
    element_id: int,
    button: str = "left",
    clicks: int = 1,
    provider: Any,
    store: StateStore,
    _input=input_mod,
    _find_by_selector: Callable = _default_find_by_selector,
) -> dict[str, Any]:
    el, live = _resolve_element(
        element_id=element_id, provider=provider, store=store,
        find_by_selector=_find_by_selector,
    )
    bounds = Rect(*live.bounds)
    cx, cy = center_of(bounds)
    _input.click(cx, cy, button=button, clicks=clicks)
    return {
        "clicked": f"{el.control_type} \"{el.name}\"" if el.name else el.control_type,
        "state_changed_hint": True,
    }


def scroll(
    *,
    element_id: int | None,
    direction: str,
    amount: int = 3,
    provider: Any,
    store: StateStore,
    _input=input_mod,
    _find_by_selector: Callable = _default_find_by_selector,
) -> dict[str, Any]:
    if direction not in ("up", "down", "left", "right"):
        raise ComputerMCPError(
            ErrorCategory.INVALID_INPUT,
            f"direction must be one of up/down/left/right, got {direction!r}",
        )
    if element_id is not None:
        _, live = _resolve_element(
            element_id=element_id, provider=provider, store=store,
            find_by_selector=_find_by_selector,
        )
        cx, cy = center_of(Rect(*live.bounds))
        _input.move_to(cx, cy)
    _input.scroll(direction, amount)
    return {"state_changed_hint": True}


def drag(
    *,
    from_element_id: int,
    to_element_id: int,
    button: str = "left",
    provider: Any,
    store: StateStore,
    _input=input_mod,
    _find_by_selector: Callable = _default_find_by_selector,
) -> dict[str, Any]:
    _, src = _resolve_element(
        element_id=from_element_id, provider=provider, store=store,
        find_by_selector=_find_by_selector,
    )
    _, dst = _resolve_element(
        element_id=to_element_id, provider=provider, store=store,
        find_by_selector=_find_by_selector,
    )
    sx, sy = center_of(Rect(*src.bounds))
    dx, dy = center_of(Rect(*dst.bounds))
    _input.drag((sx, sy), (dx, dy), button=button)
    return {"state_changed_hint": True}
