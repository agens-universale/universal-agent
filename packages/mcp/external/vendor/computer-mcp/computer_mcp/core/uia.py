"""UIA tree walking and element lookup.

Abstracts the actual UIA provider behind a Protocol so tests can inject fakes
and the Windows-only implementation lives in uia_windows.py.
"""

from __future__ import annotations

import datetime
import sys
from collections.abc import Iterable
from typing import Any, Protocol

from .uia_types import UIAElement, UIASnapshot


def _get_foreground_hwnd() -> int:
    """Win32 GetForegroundWindow wrapper. Returns 0 on non-Windows or if no foreground."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        return 0


class UIAProvider(Protocol):
    def desktop_root(self) -> Any: ...
    def iter_children(self, node: Any) -> Iterable[Any]: ...
    def is_visible(self, node: Any) -> bool: ...
    def find_by_runtime_id(self, rid: str, scope_hwnd: int | None = None) -> Any | None: ...
    def node_from_hwnd(self, hwnd: int) -> Any | None: ...


def _node_attrs(node: Any) -> dict[str, Any]:
    return {
        "runtime_id": getattr(node, "runtime_id", ""),
        "automation_id": getattr(node, "automation_id", "") or "",
        "name": getattr(node, "name", "") or "",
        "control_type": getattr(node, "control_type", "") or "",
        "bounds": tuple(getattr(node, "bounds", (0, 0, 0, 0))),
        "value": getattr(node, "value", "") or "",
    }


# Containers whose inner TextControl/ImageControl are visual labels rather
# than independent clickable elements. In Windows Calculator, each digit /
# operator Button holds a child TextControl — when the model picked the
# inner TextControl as element_id, the click missed the Button entirely.
_BUTTON_LIKE_CONTAINERS = frozenset({
    "ButtonControl",
    "ListItemControl",
    "MenuItemControl",
    "TabItemControl",
    "HyperlinkControl",
})
_DECORATIVE_CHILDREN = frozenset({"TextControl", "ImageControl"})


def _walk_tree(
    provider: UIAProvider,
    root_node: Any,
    *,
    include_invisible: bool,
) -> UIASnapshot:
    """Build a UIASnapshot by DFS from the given root node."""
    elements: list[UIAElement] = []
    windows: list[dict[str, Any]] = []
    counter = [0]

    def walk(
        node: Any,
        parent_id: int | None,
        parent_path: str,
        parent_ct: str = "",
        parent_name: str = "",
    ) -> int:
        if not include_invisible and not provider.is_visible(node):
            return -1
        attrs = _node_attrs(node)
        ct = str(attrs["control_type"])
        name = str(attrs["name"])
        # Hide decorative labels (TextControl / ImageControl) that are
        # children of clickable containers (Button, ListItem, ...) when
        # they carry no info beyond the parent: empty name, or same name
        # as the parent. Keeps top-level Text controls and labels with
        # distinct dynamic content.
        if (
            ct in _DECORATIVE_CHILDREN
            and parent_ct in _BUTTON_LIKE_CONTAINERS
            and (not name or name == parent_name)
        ):
            return -1
        this_id = counter[0]
        counter[0] += 1
        my_path = f"{parent_path}/{name}" if parent_path else name
        el = UIAElement(
            id=this_id,
            runtime_id=str(attrs["runtime_id"]),
            automation_id=str(attrs["automation_id"]),
            name=name,
            control_type=ct,
            bounds=attrs["bounds"],
            parent_id=parent_id,
            parent_path=parent_path,
            children_ids=[],
            value=str(attrs["value"]),
        )
        elements.append(el)

        if el.control_type == "Window":
            windows.append({
                "id": this_id,
                "title": el.name,
                "bounds": list(el.bounds),
            })

        for child in provider.iter_children(node):
            cid = walk(child, this_id, my_path, ct, name)
            if cid >= 0:
                el.children_ids.append(cid)
        return this_id

    walk(root_node, None, "")

    return UIASnapshot(
        windows=windows,
        elements=elements,
        first_window_id=windows[0]["id"] if windows else None,
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


def build_tree(provider: UIAProvider, include_invisible: bool = False) -> UIASnapshot:
    """Desktop-wide tree (backward-compatible entry point)."""
    return _walk_tree(
        provider, provider.desktop_root(), include_invisible=include_invisible,
    )


def build_tree_from_node(
    provider: UIAProvider,
    root_node: Any,
    *,
    include_invisible: bool = False,
) -> UIASnapshot:
    """Build a tree starting from the given root node."""
    return _walk_tree(provider, root_node, include_invisible=include_invisible)


def build_tree_from_foreground(
    provider: UIAProvider, include_invisible: bool = False
) -> UIASnapshot:
    hwnd = _get_foreground_hwnd()
    if hwnd:
        node = provider.node_from_hwnd(hwnd)
        if node is not None:
            snap = _walk_tree(provider, node, include_invisible=include_invisible)
            snap.scope_hwnd = hwnd
            return snap
    return _walk_tree(provider, provider.desktop_root(), include_invisible=include_invisible)


def build_tree_from_window(
    provider: UIAProvider, window_title: str, include_invisible: bool = False
) -> UIASnapshot:
    """Find a top-level window whose name contains window_title (case-insensitive),
    build the tree from there. Returns an empty snapshot if none matches."""
    needle = (window_title or "").strip().lower()
    desktop = provider.desktop_root()
    for child in provider.iter_children(desktop):
        attrs = _node_attrs(child)
        name = str(attrs.get("name") or "").lower()
        if needle and needle in name:
            snap = _walk_tree(provider, child, include_invisible=include_invisible)
            hwnd = _extract_hwnd(child)
            if hwnd:
                snap.scope_hwnd = hwnd
            return snap
    return UIASnapshot(
        windows=[], elements=[], first_window_id=None,
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


def _extract_hwnd(node: Any) -> int | None:
    """Best-effort HWND extractor. Returns None for fakes or non-window nodes."""
    for attr in ("NativeWindowHandle", "native_window_handle", "hwnd"):
        raw = getattr(node, attr, None)
        if raw:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        raw = getattr(getattr(node, "raw", None), attr, None)
        if raw:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
    return None


def find_by_runtime_id(
    provider: UIAProvider, runtime_id: str, scope_hwnd: int | None = None
) -> Any | None:
    return provider.find_by_runtime_id(runtime_id, scope_hwnd=scope_hwnd)


def find_by_selector(
    provider: UIAProvider,
    *,
    name: str,
    control_type: str,
    automation_id: str,
    parent_path: str,
    scope_hwnd: int | None = None,
) -> Any | None:
    """Walk the live tree looking for a node matching all non-empty selectors.

    parent_path is a hint; currently we do a full DFS and pick the first match.
    """
    if scope_hwnd is not None:
        root = provider.node_from_hwnd(scope_hwnd)
        if root is None:
            return None
    else:
        root = provider.desktop_root()

    def walk(node: Any):
        attrs = _node_attrs(node)
        matches_name = not name or str(attrs["name"]) == name
        matches_ct = not control_type or str(attrs["control_type"]) == control_type
        matches_aid = not automation_id or str(attrs["automation_id"]) == automation_id
        if matches_name and matches_ct and matches_aid:
            return node
        for c in provider.iter_children(node):
            r = walk(c)
            if r is not None:
                return r
        return None

    return walk(root)
