"""Прежний путь «State ↔ meta develop» — дословные копии кода до перевода
сессии на части v2 (агент-исполнитель, ``ThreadSession._persist_batch_meta``,
``ThreadMetaMapper``). Оракул для ``gc_core.persistence.session_state``:
живёт в тестах и после удаления старого пути."""

from __future__ import annotations

import copy
from typing import Any

from gc_core.execution.state import SharedStateData
from gc_core.intent_log.serialize import deserialize_list, serialize_for_meta
from gc_core.message_policy.tool_usage_notice.state import (
    PERSISTED_META_KEYS,
    hydrate_tool_usage_notice_state,
    persist_tool_usage_notice_state,
)
from gc_core.persistence.schema import (
    STATE_SCHEMA_VERSION,
    normalize_permission_mode,
    normalize_session_todo,
    normalize_working_memory,
    serialize_shared_block,
    validate_thread_meta_for_load,
)
from gc_core.persistence.thread_meta_mapper import read_optional_nonneg_int
from gc_core.plan_store import normalize_plan_block


def shared_from_meta(meta):
    validate_thread_meta_for_load(meta)
    if not meta:
        return None
    raw = meta.get("shared")
    if isinstance(raw, dict) and raw:
        return SharedStateData(
            working_memory=normalize_working_memory(raw.get("working_memory")),
            notes_md=str(raw.get("notes_md") or ""),
            fetch_url_cache=dict(raw.get("fetch_url_cache") or {}),
            confirm_rules=dict(raw.get("confirm_rules") or {}),
            session_todo=normalize_session_todo(raw.get("session_todo")),
            permission_mode=normalize_permission_mode(
                raw.get("permission_mode")
            ),
            plan=normalize_plan_block(raw.get("plan")),
            mode_announced=normalize_permission_mode(
                raw.get("mode_announced")
            ),
            plan_removed_notice=bool(raw.get("plan_removed_notice", False)),
            judge_denials_in_a_row=read_optional_nonneg_int(
                raw.get("judge_denials_in_a_row")
            )
            or 0,
            judge_denials_total=read_optional_nonneg_int(
                raw.get("judge_denials_total")
            )
            or 0,
            judge_paused=raw.get("judge_paused") is True,
        )
    return None


def hydrate(state, meta) -> None:
    from gc_core.usage.account import UsageAccount

    state.init_user_intent_log(meta.get("user_intent_log"))
    state.init_current_user_intent(meta.get("current_user_intent"))
    state.init_user_turn_seq(meta.get("user_turn_seq"))
    state.infer_user_turn_seq_if_missing()
    hydrate_tool_usage_notice_state(state, meta)
    state.dialog_steps_offset = int(meta.get("dialog_steps_offset", 0) or 0)
    state.inbox_seq = int(meta.get("inbox_seq", 0) or 0)
    state.last_main_prompt_tokens = read_optional_nonneg_int(
        meta.get("last_main_prompt_tokens")
    )
    state.last_main_prompt_message_count = read_optional_nonneg_int(
        meta.get("last_main_prompt_message_count")
    )
    state.usage = UsageAccount.from_meta(meta.get("token_usage"))


def _merge_state(meta, state):
    out = dict(meta)
    shared = getattr(state, "shared", None)
    if isinstance(shared, SharedStateData):
        out["shared"] = serialize_shared_block(shared)
    out["user_intent_log"] = serialize_for_meta(state.user_intent_log or [])
    out["current_user_intent"] = serialize_for_meta(
        state.current_user_intent or []
    )
    out["user_turn_seq"] = int(state.user_turn_seq or 0)
    out["dialog_steps_offset"] = int(state.dialog_steps_offset or 0)
    inbox_seq = int(state.inbox_seq or 0)
    if inbox_seq:
        out["inbox_seq"] = inbox_seq
    out["last_main_prompt_tokens"] = read_optional_nonneg_int(
        state.last_main_prompt_tokens
    )
    out["last_main_prompt_message_count"] = read_optional_nonneg_int(
        state.last_main_prompt_message_count
    )
    out["token_usage"] = state.usage.to_meta()
    out["state_schema_version"] = STATE_SCHEMA_VERSION
    return out


def _copy_session_fields(base, session_meta) -> None:
    if isinstance(session_meta.get("shared"), dict):
        base["shared"] = session_meta["shared"]
    if isinstance(session_meta.get("last_backend_response_id"), str):
        base["last_backend_response_id"] = session_meta[
            "last_backend_response_id"
        ]
    for key in ("user_intent_log", "current_user_intent"):
        if isinstance(session_meta.get(key), list):
            base[key] = serialize_for_meta(
                deserialize_list(session_meta.get(key))
            )
    if "user_turn_seq" in session_meta:
        base["user_turn_seq"] = int(session_meta.get("user_turn_seq") or 0)
    for counter in ("dialog_steps_offset", "inbox_seq"):
        if counter in session_meta:
            base[counter] = int(session_meta.get(counter) or 0)
    for key in ("last_main_prompt_tokens", "last_main_prompt_message_count"):
        if key in session_meta:
            base[key] = read_optional_nonneg_int(session_meta.get(key))
    if isinstance(session_meta.get("token_usage"), dict):
        base["token_usage"] = session_meta["token_usage"]


def meta_after_step(
    disk: dict,
    session_meta: dict,
    state: Any,
    *,
    rendered: str | None,
    backend: str,
    previous_response_id: str | None,
) -> dict:
    """Meta, которую прежний путь записал бы шагом без новых сообщений."""
    sm = copy.deepcopy(session_meta)
    key = "chat_completions_system_prompt_rendered"
    if backend == "chat_completions" and rendered and rendered.strip():
        sm[key] = rendered
    sm = _merge_state(sm, state)
    sm["backend_api_mode"] = backend
    persist_tool_usage_notice_state(state, sm)
    last = getattr(state, "last_backend_response_id", None)
    if isinstance(last, str) and last:
        sm["last_backend_response_id"] = last
    base = copy.deepcopy(disk)
    base["previous_response_id"] = previous_response_id
    base["backend_api_mode"] = backend
    _copy_session_fields(base, sm)
    if backend == "chat_completions":
        prompt = sm.get(key)
        if isinstance(prompt, str) and prompt.strip():
            base[key] = prompt
    else:
        base.pop(key, None)
    for name in PERSISTED_META_KEYS:
        if name in sm:
            base[name] = sm[name]
    return base


def _carry_meta_string(new_meta, existing_meta, key) -> None:
    value = existing_meta.get(key)
    if isinstance(value, str) and value.strip():
        new_meta[key] = value


def compact_meta(
    existing_meta: dict,
    summary_payload: dict,
    *,
    now_iso: str,
    backend: str,
    compact_count: int,
) -> dict:
    """Поля состояния прежнего ``ThreadSession._compact_meta`` (без tui,
    runtime и штампов хранилища — их считают отдельно)."""
    new_meta = dict(existing_meta)
    new_meta.update(
        {
            "previous_response_id": None,
            "backend_api_mode": backend,
            "messages_count": compact_count,
            "last_summarized_at": now_iso,
        }
    )
    shared_data = summary_payload.get("shared")
    if isinstance(shared_data, dict):
        new_meta["shared"] = shared_data
    try:
        offset = int(summary_payload.get("dialog_steps_offset", 0))
    except (TypeError, ValueError):
        offset = int(existing_meta.get("dialog_steps_offset", 0) or 0)
    new_meta["dialog_steps_offset"] = max(0, offset)
    new_meta["last_main_prompt_tokens"] = read_optional_nonneg_int(
        summary_payload.get("last_main_prompt_tokens")
    )
    new_meta["last_main_prompt_message_count"] = read_optional_nonneg_int(
        summary_payload.get("last_main_prompt_message_count")
    )
    log = summary_payload.get("user_intent_log")
    if isinstance(log, list):
        new_meta["user_intent_log"] = serialize_for_meta(deserialize_list(log))
    elif isinstance(existing_meta.get("user_intent_log"), list):
        new_meta["user_intent_log"] = serialize_for_meta(
            deserialize_list(existing_meta.get("user_intent_log"))
        )
    if isinstance(existing_meta.get("current_user_intent"), list):
        new_meta["current_user_intent"] = serialize_for_meta(
            deserialize_list(existing_meta.get("current_user_intent"))
        )
    seq = summary_payload.get("user_turn_seq")
    if seq is not None:
        new_meta["user_turn_seq"] = int(seq or 0)
    elif "user_turn_seq" in existing_meta:
        new_meta["user_turn_seq"] = int(
            existing_meta.get("user_turn_seq") or 0
        )
    _carry_meta_string(
        new_meta, existing_meta, "chat_completions_system_prompt_rendered"
    )
    _carry_meta_string(new_meta, existing_meta, "last_backend_response_id")
    return new_meta
