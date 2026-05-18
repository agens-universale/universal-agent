"""Data structures for UIA snapshots and their text rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UIAElement:
    id: int
    runtime_id: str
    automation_id: str
    name: str
    control_type: str
    bounds: tuple[int, int, int, int]  # (x, y, w, h)
    parent_id: int | None
    parent_path: str
    children_ids: list[int] = field(default_factory=list)
    # Current value of an input-like control (EditControl, ComboBox, etc.).
    # Empty for controls that don't carry a value distinct from their name.
    value: str = ""


@dataclass
class UIASnapshot:
    windows: list[dict[str, Any]]
    elements: list[UIAElement]
    # First window discovered in DFS order; NOT a true focused-window detection.
    first_window_id: int | None
    timestamp: str = ""
    scope_hwnd: int | None = None

    def by_id(self, eid: int) -> UIAElement | None:
        for el in self.elements:
            if el.id == eid:
                return el
        return None


def _depth(el: UIAElement, by_id: dict[int, UIAElement]) -> int:
    d = 0
    cur = el
    while cur.parent_id is not None and cur.parent_id in by_id:
        d += 1
        cur = by_id[cur.parent_id]
    return d


def render_tree_text(snap: UIASnapshot) -> str:
    by_id = {el.id: el for el in snap.elements}
    lines: list[str] = []
    for el in snap.elements:
        indent = "  " * _depth(el, by_id)
        name_q = f' "{el.name}"' if el.name else ""
        value_q = f' value="{el.value}"' if el.value else ""
        lines.append(f"{indent}[{el.id}] {el.control_type}{name_q}{value_q}")
    return "\n".join(lines)


def snapshot_to_dict(snap: UIASnapshot) -> dict[str, Any]:
    return {
        "windows": snap.windows,
        "first_window_id": snap.first_window_id,
        "timestamp": snap.timestamp,
        "scope_hwnd": snap.scope_hwnd,
        "elements": [
            {
                "id": e.id,
                "runtime_id": e.runtime_id,
                "automation_id": e.automation_id,
                "name": e.name,
                "control_type": e.control_type,
                "bounds": list(e.bounds),
                "parent_id": e.parent_id,
                "parent_path": e.parent_path,
                "children_ids": list(e.children_ids),
                "value": e.value,
            }
            for e in snap.elements
        ],
    }


def snapshot_from_dict(d: dict[str, Any]) -> UIASnapshot:
    elements = [
        UIAElement(
            id=e["id"],
            runtime_id=e["runtime_id"],
            automation_id=e["automation_id"],
            name=e["name"],
            control_type=e["control_type"],
            bounds=tuple(e["bounds"]),
            parent_id=e["parent_id"],
            parent_path=e["parent_path"],
            children_ids=list(e["children_ids"]),
            value=e.get("value", "") or "",
        )
        for e in d.get("elements", [])
    ]
    return UIASnapshot(
        windows=list(d.get("windows", [])),
        elements=elements,
        first_window_id=d.get("first_window_id"),
        timestamp=d.get("timestamp", ""),
        scope_hwnd=d.get("scope_hwnd"),
    )
