"""Serialize / deserialize State snapshots (v2)."""

from __future__ import annotations

from typing import Any, Dict

from persistence.schema import (
    STATE_SCHEMA_VERSION,
    normalize_working_memory,
    serialize_shared_block,
    validate_state_snapshot_for_load,
)
from states import SharedStateData, State
from tools.stored_outputs.tool_call_outputs_store import ToolCallOutputsStore


def _deserialize_state_payload(raw: Dict[str, Any]) -> State:
    state = State.__new__(State)
    state.__dict__ = dict(raw)

    shared_raw = state.__dict__.get("shared")
    if isinstance(shared_raw, dict):
        state.shared = SharedStateData(
            working_memory=normalize_working_memory(shared_raw.get("working_memory")),
            notes_md=str(shared_raw.get("notes_md") or ""),
            fetch_url_cache=shared_raw.get("fetch_url_cache") or {},
        )

    store_raw = state.__dict__.get("tool_call_outputs_store")
    if isinstance(store_raw, dict):
        state.tool_call_outputs_store = ToolCallOutputsStore.from_dict(store_raw)
    elif hasattr(store_raw, "to_dict"):
        # Already hydrated (should not happen in normal deserialize path).
        state.tool_call_outputs_store = store_raw

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
        if not isinstance(payload, dict):
            raise TypeError("StateSerializer.deserialize expects dict")
        validate_state_snapshot_for_load(payload)
        return _deserialize_state_payload(payload)
