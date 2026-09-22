"""Guard persist после user-stop: обычный хвост discard, pairing A/B — нет."""

from __future__ import annotations

from typing import Any


def is_persist_discarded(state: Any, *, pairing: bool = False) -> bool:
    """True → не писать в thread. ``pairing=True`` — закрывающие A/B при user-stop."""
    if pairing or getattr(state, "allow_pairing_persist", False):
        return False
    return bool(getattr(state, "discard_writes_after_stop", False) and getattr(state, "stop_requested", False))


__all__ = ["is_persist_discarded"]
