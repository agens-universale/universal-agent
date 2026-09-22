"""Versioned state snapshot contract (v2)."""

from __future__ import annotations

from typing import Any, Mapping

from gc_core.intent_log.serialize import deserialize_list

STATE_SCHEMA_VERSION = "v2"

WORKING_MEMORY_REGISTERS = ("goal", "constraints", "state", "knowledge")

TODO_STATUSES = ("pending", "in_progress", "completed")


class StateSchemaError(ValueError):
    """Invalid or unsupported state / thread meta payload."""


def _is_non_empty_mapping(value: Any) -> bool:
    return isinstance(value, Mapping) and bool(value)


def _intent_records_non_empty(items: Any) -> bool:
    if not isinstance(items, list) or not items:
        return False
    try:
        return bool(deserialize_list(items))
    except (TypeError, ValueError):
        return False


def meta_has_persisted_agent_state(meta: Mapping[str, Any]) -> bool:
    """True when meta carries restorable v2 agent fields (not just thread bookkeeping).

    Call after ``reject_legacy_memory_keys`` on load paths; legacy ``memory_files``
    is never a valid persisted-state signal in v2.
    """
    if _is_non_empty_mapping(meta.get("shared")):
        return True
    if _intent_records_non_empty(meta.get("user_intent_log")):
        return True
    return bool(_intent_records_non_empty(meta.get("current_user_intent")))


def reject_legacy_memory_keys(payload: Mapping[str, Any], *, path: str = "root") -> None:
    if "memory_files" in payload:
        raise StateSchemaError(
            f"Legacy field 'memory_files' is not supported at {path} "
            f"(state_schema_version={STATE_SCHEMA_VERSION!r} required)."
        )
    shared = payload.get("shared")
    if isinstance(shared, Mapping) and "memory_files" in shared:
        raise StateSchemaError(
            f"Legacy field 'shared.memory_files' is not supported "
            f"(state_schema_version={STATE_SCHEMA_VERSION!r} required)."
        )


def require_state_schema_version(payload: Mapping[str, Any], *, context: str) -> None:
    version = payload.get("state_schema_version")
    if version != STATE_SCHEMA_VERSION:
        raise StateSchemaError(
            f"Unsupported {context}: state_schema_version={version!r}, "
            f"expected {STATE_SCHEMA_VERSION!r}."
        )


def validate_thread_meta_for_load(meta: Mapping[str, Any]) -> None:
    if not meta:
        return
    reject_legacy_memory_keys(meta)
    if not meta_has_persisted_agent_state(meta):
        return
    require_state_schema_version(meta, context="thread meta")


def validate_state_snapshot_for_load(payload: Mapping[str, Any]) -> None:
    if not payload:
        raise StateSchemaError("State snapshot payload must be a non-empty dict.")
    reject_legacy_memory_keys(payload)
    require_state_schema_version(payload, context="state snapshot")


def normalize_working_memory(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for name in WORKING_MEMORY_REGISTERS:
        value = raw.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[name] = text
    return out


def normalize_session_todo(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        content = str(item.get("content") or "").strip()
        status = item.get("status")
        if not content or status not in TODO_STATUSES:
            continue
        out.append({"content": content, "status": status})
    return out


def serialize_shared_block(shared: Any) -> dict[str, Any]:
    # JSON key ``working_memory`` — legacy persist name; same dict as ``session_memory`` tool.
    working_memory = normalize_working_memory(getattr(shared, "working_memory", None))
    notes_md = str(getattr(shared, "notes_md", "") or "")
    fetch_url_cache = getattr(shared, "fetch_url_cache", None)
    cache: dict[str, Any] = {}
    if isinstance(fetch_url_cache, Mapping):
        cache = dict(fetch_url_cache)
    confirm_rules = getattr(shared, "confirm_rules", None)
    rules: dict[str, Any] = dict(confirm_rules) if isinstance(confirm_rules, Mapping) else {}
    return {
        "working_memory": working_memory,
        "notes_md": notes_md,
        "fetch_url_cache": cache,
        "confirm_rules": rules,
        "session_todo": normalize_session_todo(getattr(shared, "session_todo", None)),
    }
