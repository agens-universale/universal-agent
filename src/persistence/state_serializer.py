"""Serialize / deserialize State snapshots (v2)."""

from __future__ import annotations

from typing import Any, Dict, Mapping

from persistence.schema import (
    STATE_SCHEMA_VERSION,
    normalize_working_memory,
    serialize_shared_block,
    validate_state_snapshot_for_load,
)
from states import SharedStateData, State
from tools.stored_outputs.tool_call_outputs_store import ToolCallOutputsStore


def load_state_from_snapshot(payload: Mapping[str, Any]) -> State:
    """
    Restore runtime ``State`` from a v2 snapshot (session dump / ``StateRepository``).

    Lives in the persistence adapter: ``states.State`` is constructed only via ``__init__``
    at runtime; snapshots bypass ``__init__`` to preserve ``state_id``, messages, and
    metadata without re-running project discovery.

    Nested fields are hydrated explicitly: ``shared`` → ``SharedStateData``,
    ``tool_call_outputs_store`` → ``ToolCallOutputsStore``. New nested snapshot fields:
    extend this function and ``tests/test_state_persistence_v2.py``.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("load_state_from_snapshot expects a mapping")
    snapshot = dict(payload)
    validate_state_snapshot_for_load(snapshot)

    state = State.__new__(State)
    state.__dict__ = snapshot

    shared_raw = state.__dict__.get("shared")
    if isinstance(shared_raw, dict):
        state.shared = SharedStateData(
            working_memory=normalize_working_memory(shared_raw.get("working_memory")),
            notes_md=str(shared_raw.get("notes_md") or ""),
            fetch_url_cache=dict(shared_raw.get("fetch_url_cache") or {}),
        )
    elif not isinstance(shared_raw, SharedStateData):
        state.shared = SharedStateData()

    store_raw = state.__dict__.get("tool_call_outputs_store")
    if isinstance(store_raw, dict):
        state.tool_call_outputs_store = ToolCallOutputsStore.from_dict(store_raw)
    elif not isinstance(store_raw, ToolCallOutputsStore):
        state.tool_call_outputs_store = ToolCallOutputsStore()

    return state


def _serialize_tool_call_outputs_store(store: Any) -> dict[str, Any] | Any:
    to_dict = getattr(store, "to_dict", None)
    if callable(to_dict) and not isinstance(store, dict):
        return to_dict()
    return store


class StateSerializer:
    @staticmethod
    def serialize(state: State) -> Dict[str, Any]:
        raw = vars(state).copy()

        shared = raw.get("shared")
        if isinstance(shared, SharedStateData) or (
            shared is not None
            and not isinstance(shared, dict)
            and hasattr(shared, "working_memory")
        ):
            raw["shared"] = serialize_shared_block(shared)

        store = raw.get("tool_call_outputs_store")
        raw["tool_call_outputs_store"] = _serialize_tool_call_outputs_store(store)

        raw["state_schema_version"] = STATE_SCHEMA_VERSION
        return raw

    @staticmethod
    def deserialize(payload: Dict[str, Any]) -> State:
        """Load ``State`` from v2 snapshot; see ``load_state_from_snapshot``."""
        return load_state_from_snapshot(payload)
