"""Active child input scope: mid-run user prose → executing subagent.

Parent AgentExecutor.add_message routes here when a child is registered.
Child run_agent drains via get_pending_messages (same contract as parent).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

_lock = threading.Lock()
_active: Optional["ChildInputScope"] = None


class ChildInputScope:
    """Thread-safe pending queue for one executing subagent."""

    def __init__(self, *, parent_call_id: str) -> None:
        self.parent_call_id = parent_call_id or ""
        self._q_lock = threading.Lock()
        self._pending: List[Dict[str, Any]] = []
        self._on_enqueue: Optional[Callable[[Dict[str, Any]], None]] = None

    def set_on_enqueue(
        self, callback: Optional[Callable[[Dict[str, Any]], None]]
    ) -> None:
        self._on_enqueue = callback

    def enqueue(self, message: Dict[str, Any]) -> None:
        with self._q_lock:
            self._pending.append(message)
        cb = self._on_enqueue
        if cb is not None:
            try:
                cb(message)
            except Exception:
                pass

    def drain(self) -> List[Dict[str, Any]]:
        with self._q_lock:
            out = self._pending[:]
            self._pending.clear()
            return out


@contextmanager
def activate_child_input(*, parent_call_id: str) -> Iterator[ChildInputScope]:
    """Register this child as the active input scope for the duration of the block."""
    scope = ChildInputScope(parent_call_id=parent_call_id)
    global _active
    with _lock:
        _active = scope
    try:
        yield scope
    finally:
        with _lock:
            if _active is scope:
                _active = None


def try_enqueue_user_message(message: Dict[str, Any]) -> bool:
    """Enqueue into active child if any. True = consumed by child (not parent)."""
    with _lock:
        scope = _active
    if scope is None:
        return False
    scope.enqueue(message)
    return True


def is_child_input_active() -> bool:
    with _lock:
        return _active is not None


def active_parent_call_id() -> Optional[str]:
    with _lock:
        scope = _active
    if scope is None:
        return None
    pid = scope.parent_call_id
    return pid if pid else None


def reset_active_scope_for_tests() -> None:
    """Test helper: drop any leaked active registration."""
    global _active
    with _lock:
        _active = None


__all__ = [
    "ChildInputScope",
    "activate_child_input",
    "active_parent_call_id",
    "is_child_input_active",
    "reset_active_scope_for_tests",
    "try_enqueue_user_message",
]
