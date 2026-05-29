"""Map between runtime State fields and thread session meta (v2)."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from persistence.schema import (
    STATE_SCHEMA_VERSION,
    StateSchemaError,
    normalize_working_memory,
    reject_legacy_memory_keys,
    serialize_shared_block,
    validate_thread_meta_for_load,
)
from states import SharedStateData, State


class ThreadMetaMapper:
    @staticmethod
    def shared_from_meta(meta: Mapping[str, Any]) -> Optional[SharedStateData]:
        validate_thread_meta_for_load(meta)
        if not meta:
            return None

        shared_raw = meta.get("shared")
        if isinstance(shared_raw, Mapping) and shared_raw:
            return SharedStateData(
                working_memory=normalize_working_memory(shared_raw.get("working_memory")),
                notes_md=str(shared_raw.get("notes_md") or ""),
                fetch_url_cache=dict(shared_raw.get("fetch_url_cache") or {}),
            )

        return None

    @staticmethod
    def merge_state_into_meta(meta: Dict[str, Any], state: State) -> Dict[str, Any]:
        out = dict(meta)
        shared = getattr(state, "shared", None)
        if isinstance(shared, SharedStateData):
            out["shared"] = serialize_shared_block(shared)
        out["user_intent_log"] = list(getattr(state, "user_intent_log", []) or [])
        out["current_user_intent"] = list(getattr(state, "current_user_intent", []) or [])
        out["dialog_steps_offset"] = int(getattr(state, "dialog_steps_offset", 0) or 0)
        out["state_schema_version"] = STATE_SCHEMA_VERSION
        reject_legacy_memory_keys(out)
        return out

    @staticmethod
    def merge_shared_into_meta(meta: Dict[str, Any], shared: SharedStateData) -> Dict[str, Any]:
        out = dict(meta)
        out["shared"] = serialize_shared_block(shared)
        out["state_schema_version"] = STATE_SCHEMA_VERSION
        reject_legacy_memory_keys(out)
        return out

    @staticmethod
    def ensure_v2_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(meta)
        out["state_schema_version"] = STATE_SCHEMA_VERSION
        reject_legacy_memory_keys(out)
        return out
