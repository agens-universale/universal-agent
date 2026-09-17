"""get_state and wait tools."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from ..core.state_store import StateStore
from ..core.uia import (
    build_tree as _default_build_tree,
)
from ..core.uia import (
    build_tree_from_foreground as _default_build_tree_from_foreground,
)
from ..core.uia import (
    build_tree_from_window as _default_build_tree_from_window,
)
from ..core.uia_types import UIASnapshot, render_tree_text, snapshot_to_dict
from ..errors import ComputerMCPError, ErrorCategory

_SHELL_NAMES = {
    "панель задач", "taskbar", "program manager", "shell_traywnd",
}

_INTERACTIVE_CONTROL_TYPES = {
    "Edit", "Button", "ComboBox", "CheckBox", "RadioButton", "Hyperlink",
    "ListItem", "TreeItem", "MenuItem", "Tab", "Slider", "Spinner",
}


def get_state(
    *,
    provider: Any,
    store: StateStore,
    scope: Literal["desktop", "foreground", "window"] = "foreground",
    window_title: str | None = None,
    max_depth: int | None = None,
    include_invisible: bool = False,
    exclude_shell: bool = True,
    only_interactive: bool = False,
    _build_tree: Callable = _default_build_tree,
    _build_tree_from_foreground: Callable = _default_build_tree_from_foreground,
    _build_tree_from_window: Callable = _default_build_tree_from_window,
) -> dict[str, Any]:
    """Capture the UIA tree (scoped), persist snapshot, return text render + meta."""
    if scope == "window" and not window_title:
        raise ComputerMCPError(
            ErrorCategory.INVALID_INPUT,
            "scope='window' requires window_title",
        )

    if scope == "foreground":
        snap = _build_tree_from_foreground(provider, include_invisible=include_invisible)
    elif scope == "window":
        snap = _build_tree_from_window(
            provider, window_title=window_title, include_invisible=include_invisible,
        )
    else:  # desktop
        snap = _build_tree(provider, include_invisible=include_invisible)

    if exclude_shell and scope == "desktop":
        snap = _filter_shell(snap)
    if max_depth is not None:
        snap = _filter_max_depth(snap, max_depth)
    if only_interactive:
        snap = _filter_only_interactive(snap)

    store.save(snapshot_to_dict(snap))
    return {
        "tree_text": render_tree_text(snap),
        "windows": snap.windows,
        "first_window_id": snap.first_window_id,
        "timestamp": snap.timestamp,
    }


def _filter_shell(snap: UIASnapshot) -> UIASnapshot:
    """Drop top-level panes named like Windows shell (taskbar, Program Manager)."""
    kept_ids: set[int] = set()
    dropped_ids: set[int] = set()
    by_id = {el.id: el for el in snap.elements}
    for el in snap.elements:
        if el.parent_id is None:
            kept_ids.add(el.id)
            continue
        if el.name.lower() in _SHELL_NAMES:
            dropped_ids.add(el.id)
            continue
        anc = el.parent_id
        skipped = False
        while anc is not None:
            if anc in dropped_ids:
                dropped_ids.add(el.id)
                skipped = True
                break
            anc_el = by_id.get(anc)
            anc = anc_el.parent_id if anc_el else None
        if not skipped:
            kept_ids.add(el.id)
    new_elements = [el for el in snap.elements if el.id in kept_ids]
    for el in new_elements:
        el.children_ids = [c for c in el.children_ids if c in kept_ids]
    new_windows = [w for w in snap.windows if w.get("id") in kept_ids]
    return UIASnapshot(
        windows=new_windows,
        elements=new_elements,
        first_window_id=snap.first_window_id if snap.first_window_id in kept_ids else None,
        timestamp=snap.timestamp,
    )


def _filter_max_depth(snap: UIASnapshot, max_depth: int) -> UIASnapshot:
    by_id = {el.id: el for el in snap.elements}
    depth_of: dict[int, int] = {}

    def depth(eid: int) -> int:
        if eid in depth_of:
            return depth_of[eid]
        el = by_id.get(eid)
        if el is None or el.parent_id is None:
            depth_of[eid] = 0
            return 0
        d = depth(el.parent_id) + 1
        depth_of[eid] = d
        return d

    kept = {el.id for el in snap.elements if depth(el.id) <= max_depth}
    new_elements = [el for el in snap.elements if el.id in kept]
    for el in new_elements:
        el.children_ids = [c for c in el.children_ids if c in kept]
    new_windows = [w for w in snap.windows if w.get("id") in kept]
    return UIASnapshot(
        windows=new_windows, elements=new_elements,
        first_window_id=snap.first_window_id if snap.first_window_id in kept else None,
        timestamp=snap.timestamp,
    )


def _filter_only_interactive(snap: UIASnapshot) -> UIASnapshot:
    """Keep interactive leaves + all their ancestors (preserve hierarchy)."""
    by_id = {el.id: el for el in snap.elements}
    interactive_ids = {
        el.id for el in snap.elements if el.control_type in _INTERACTIVE_CONTROL_TYPES
    }
    kept: set[int] = set(interactive_ids)
    for eid in list(interactive_ids):
        anc = by_id[eid].parent_id
        while anc is not None:
            kept.add(anc)
            anc_el = by_id.get(anc)
            anc = anc_el.parent_id if anc_el else None
    new_elements = [el for el in snap.elements if el.id in kept]
    for el in new_elements:
        el.children_ids = [c for c in el.children_ids if c in kept]
    new_windows = [w for w in snap.windows if w.get("id") in kept]
    return UIASnapshot(
        windows=new_windows, elements=new_elements,
        first_window_id=snap.first_window_id if snap.first_window_id in kept else None,
        timestamp=snap.timestamp,
    )


def wait(*, seconds: float) -> dict[str, Any]:
    if seconds < 0 or seconds > 30:
        raise ComputerMCPError(
            ErrorCategory.INVALID_INPUT,
            f"wait seconds must be between 0 and 30, got {seconds}",
        )
    time.sleep(seconds)
    return {"waited_seconds": seconds}
