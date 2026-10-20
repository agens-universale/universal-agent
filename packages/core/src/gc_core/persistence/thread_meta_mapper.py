"""Map between runtime State fields and thread session meta (v2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from gc_core.execution.state import SharedStateData
from gc_core.intent_log.serialize import serialize_for_meta
from gc_core.persistence.schema import (
    STATE_SCHEMA_VERSION,
    normalize_permission_mode,
    normalize_session_todo,
    normalize_working_memory,
    reject_legacy_memory_keys,
    serialize_shared_block,
    validate_thread_meta_for_load,
)
from gc_core.plan_store import normalize_plan_block

if TYPE_CHECKING:
    from gc_core.states import State


def read_optional_nonneg_int(value: Any) -> Optional[int]:
    """Число есть — неотрицательный int. null, отсутствие и не-число — None.

    Не ``int(value or 0)``: ноль и «числа нет» здесь разные.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


class ThreadMetaMapper:
    @staticmethod
    def shared_from_meta(meta: Mapping[str, Any]) -> Optional[SharedStateData]:
        validate_thread_meta_for_load(meta)
        if not meta:
            return None

        shared_raw = meta.get("shared")
        if isinstance(shared_raw, Mapping) and shared_raw:
            return SharedStateData(
                working_memory=normalize_working_memory(
                    shared_raw.get("working_memory")
                ),
                notes_md=str(shared_raw.get("notes_md") or ""),
                fetch_url_cache=dict(shared_raw.get("fetch_url_cache") or {}),
                confirm_rules=dict(shared_raw.get("confirm_rules") or {}),
                session_todo=normalize_session_todo(
                    shared_raw.get("session_todo")
                ),
                permission_mode=normalize_permission_mode(
                    shared_raw.get("permission_mode")
                ),
                plan=normalize_plan_block(shared_raw.get("plan")),
                mode_announced=normalize_permission_mode(
                    shared_raw.get("mode_announced")
                ),
                plan_removed_notice=bool(
                    shared_raw.get("plan_removed_notice", False)
                ),
                judge_denials_in_a_row=read_optional_nonneg_int(
                    shared_raw.get("judge_denials_in_a_row")
                )
                or 0,
                judge_denials_total=read_optional_nonneg_int(
                    shared_raw.get("judge_denials_total")
                )
                or 0,
                judge_paused=shared_raw.get("judge_paused") is True,
            )

        return None

    @staticmethod
    def merge_state_into_meta(
        meta: Dict[str, Any], state: State
    ) -> Dict[str, Any]:
        out = dict(meta)
        shared = getattr(state, "shared", None)
        if isinstance(shared, SharedStateData):
            out["shared"] = serialize_shared_block(shared)
        out["user_intent_log"] = serialize_for_meta(
            getattr(state, "user_intent_log", []) or []
        )
        out["current_user_intent"] = serialize_for_meta(
            getattr(state, "current_user_intent", []) or []
        )
        out["user_turn_seq"] = int(getattr(state, "user_turn_seq", 0) or 0)
        out["dialog_steps_offset"] = int(
            getattr(state, "dialog_steps_offset", 0) or 0
        )
        # Только у тредов с входящим потоком: meta остальных — как раньше.
        inbox_seq = int(getattr(state, "inbox_seq", 0) or 0)
        if inbox_seq:
            out["inbox_seq"] = inbox_seq
        out["last_main_prompt_tokens"] = read_optional_nonneg_int(
            getattr(state, "last_main_prompt_tokens", None)
        )
        out["last_main_prompt_message_count"] = read_optional_nonneg_int(
            getattr(state, "last_main_prompt_message_count", None)
        )
        usage = getattr(state, "usage", None)
        if usage is not None and hasattr(usage, "to_meta"):
            out["token_usage"] = usage.to_meta()
        out["state_schema_version"] = STATE_SCHEMA_VERSION
        reject_legacy_memory_keys(out)
        return out

    @staticmethod
    def read_prompt_token_fields(
        meta: Mapping[str, Any],
    ) -> tuple[Optional[int], Optional[int]]:
        """null и отсутствие ключа — None. 0 остаётся 0."""
        return (
            read_optional_nonneg_int(meta.get("last_main_prompt_tokens")),
            read_optional_nonneg_int(
                meta.get("last_main_prompt_message_count")
            ),
        )

    @staticmethod
    def merge_shared_into_meta(
        meta: Dict[str, Any], shared: SharedStateData
    ) -> Dict[str, Any]:
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
