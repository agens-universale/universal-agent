"""Windows-specific UIAProvider using the `uiautomation` Python package."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterable
from typing import Any

from ..errors import ComputerMCPError, ErrorCategory

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    import uiautomation as auto  # type: ignore
else:
    auto = None  # type: ignore

# Number of retries for transient UIA COM errors (e.g. when a modal dialog is
# still rendering). Each retry waits `_UIA_RETRY_DELAY` seconds.
_UIA_RETRIES = 2
_UIA_RETRY_DELAY = 0.2


def _children_with_retry(ctrl: Any) -> list[Any]:
    """Call GetChildren() tolerating transient COM errors.

    UIA sometimes fails while a window is still rendering (notable on modal
    dialogs). Retrying after a short pause is reliable.
    """
    last_exc: Exception | None = None
    for attempt in range(_UIA_RETRIES + 1):
        try:
            return list(ctrl.GetChildren())
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < _UIA_RETRIES:
                logger.debug(
                    "UIA GetChildren failed (attempt %d/%d): %s; retrying in %.1fs",
                    attempt + 1, _UIA_RETRIES + 1, e, _UIA_RETRY_DELAY,
                )
                time.sleep(_UIA_RETRY_DELAY)
    assert last_exc is not None
    raise last_exc


class _Wrapped:
    """Adapts a uiautomation.Control into the minimal shape our UIAProvider uses.

    We keep a reference to the live Control so callers can trigger clicks on it
    via coordinates computed from its BoundingRectangle.
    """

    __slots__ = ("_ctrl",)

    def __init__(self, ctrl: Any) -> None:
        self._ctrl = ctrl

    @property
    def raw(self) -> Any:
        return self._ctrl

    @property
    def runtime_id(self) -> str:
        try:
            rid = self._ctrl.GetRuntimeId()
            return "/".join(str(x) for x in rid) if rid else ""
        except Exception:
            return ""

    @property
    def automation_id(self) -> str:
        return getattr(self._ctrl, "AutomationId", "") or ""

    @property
    def name(self) -> str:
        return getattr(self._ctrl, "Name", "") or ""

    @property
    def control_type(self) -> str:
        return getattr(self._ctrl, "ControlTypeName", "") or ""

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        r = getattr(self._ctrl, "BoundingRectangle", None)
        if r is None:
            return (0, 0, 0, 0)
        return (int(r.left), int(r.top), int(r.width()), int(r.height()))

    @property
    def value(self) -> str:
        """Current value of input-like controls (EditControl, ComboBox, etc.).
        Empty string for controls that don't carry a value distinct from Name
        (Button, Pane, Window), which skips a per-element COM call to keep
        large snapshots fast.
        """
        ct = self.control_type
        # uiautomation returns names like "EditControl", "ComboBoxControl".
        if not any(
            marker in ct
            for marker in ("Edit", "ComboBox", "Document", "Spinner")
        ):
            return ""
        try:
            pattern = self._ctrl.GetValuePattern()
        except Exception:
            return ""
        if pattern is None:
            return ""
        try:
            raw = pattern.Value
        except Exception:
            return ""
        return str(raw) if raw else ""

    def is_offscreen(self) -> bool:
        try:
            return bool(self._ctrl.IsOffscreen)
        except Exception:
            return False


class WindowsUIAProvider:
    def __init__(self) -> None:
        if auto is None:
            raise ComputerMCPError(
                ErrorCategory.UIA_UNAVAILABLE,
                "uiautomation is only available on Windows",
            )

    def desktop_root(self) -> _Wrapped:
        return _Wrapped(auto.GetRootControl())

    def iter_children(self, node: _Wrapped) -> Iterable[_Wrapped]:
        for c in _children_with_retry(node.raw):
            yield _Wrapped(c)

    def is_visible(self, node: _Wrapped) -> bool:
        if node.is_offscreen():
            return False
        w, h = node.bounds[2], node.bounds[3]
        return w > 0 and h > 0

    def node_from_hwnd(self, hwnd: int) -> _Wrapped | None:
        if not hwnd:
            return None
        try:
            ctrl = auto.ControlFromHandle(hwnd)
        except Exception as e:  # noqa: BLE001
            logger.debug("ControlFromHandle failed for hwnd=%d: %s", hwnd, e)
            return None
        return _Wrapped(ctrl) if ctrl is not None else None

    def find_by_runtime_id(
        self, rid: str, scope_hwnd: int | None = None
    ) -> _Wrapped | None:
        if not rid:
            return None
        target = [int(x) for x in rid.split("/") if x.isdigit()]

        if scope_hwnd is not None:
            root_wrapped = self.node_from_hwnd(scope_hwnd)
            if root_wrapped is None:
                return None
            root_ctrl = root_wrapped.raw
        else:
            root_ctrl = auto.GetRootControl()

        def walk(ctrl: Any) -> Any | None:
            try:
                cur_rid = ctrl.GetRuntimeId()
            except Exception:
                cur_rid = None
            if cur_rid and list(cur_rid) == target:
                return ctrl
            try:
                children = _children_with_retry(ctrl)
            except Exception:
                children = []
            for c in children:
                r = walk(c)
                if r is not None:
                    return r
            return None

        found = walk(root_ctrl)
        return _Wrapped(found) if found is not None else None
