"""meta develop ↔ модели v2: круговой проход без потерь и точечные поля."""

import json

import pytest
from gc_core.threads.meta_map import meta_from_parts, parts_from_meta
from gc_testkit.legacy_threads import FIXTURE_ROOT

FIXTURE_METAS = sorted(
    p
    for p in (FIXTURE_ROOT / "threads").glob("meta_*.json")
    if "__pre_summary__" not in p.name
)


def _roundtrip(meta):
    return meta_from_parts(parts_from_meta(meta), base=meta)


def test_fixture_metas_are_found():
    assert len(FIXTURE_METAS) >= 7


@pytest.mark.parametrize("path", FIXTURE_METAS, ids=lambda p: p.name)
def test_fixture_meta_roundtrips_exactly(path):
    meta = json.loads(path.read_text(encoding="utf-8"))
    out = _roundtrip(meta)
    assert out == meta
    assert list(out) == list(meta), "порядок ключей сохраняется"


SYNTHETIC = {
    "system": "cli",
    "system_id": "cli",
    "schema_version": 2,
    "state_schema_version": "v2",
    "thread_id": "t",
    "created_at": "2026-10-14T12:00:00+00:00",
    "updated_at": "2026-10-14T12:00:01+00:00",
    "runtime": {
        "live_history_chars": 10,
        "live_msg_count": 2,
        "summarizing": True,
        "image_seq": 4,
        "mcp_tools": {"srv": {"status": "ok", "tools": []}},
        "trace_ids": ["a", "b"],
        "custom_runtime": 1,
    },
    "previous_response_id": None,
    "backend_api_mode": "chat_completions",
    "messages_count": 2,
    "shared": {
        "working_memory": {"goal": "g"},
        "notes_md": "n",
        "fetch_url_cache": {},
        "confirm_rules": {"x": 1},
        "session_todo": [{"content": "c", "status": "pending", "odd": 1}],
    },
    "user_intent_log": [{"turn": 1}],
    "current_user_intent": [],
    "user_turn_seq": 1,
    "dialog_steps_offset": 0,
    "last_main_prompt_tokens": None,
    "last_main_prompt_message_count": 3,
    "token_usage": {"by_category": {"main": {"input": 1}}, "currency": "RUB"},
    "project_memory_content_hash": None,
    "tool_usage_notice_cnt": {"fs_read_file": 2},
    "todo_reminder_cnt": 1,
    "project_memory_check_cnt": 5,
    "last_backend_response_id": "chatcmpl-1",
    "last_summarized_at": "2026-10-14T12:00:02+00:00",
    "last_summary_archive_suffix": "20261014_120002",
    "tui": {"title": "T", "status": "idle", "project_path": "/w"},
    "tui_summarizing": False,
    "swarm_meta": {"id": 1},
}


def test_synthetic_meta_roundtrips_exactly():
    assert _roundtrip(SYNTHETIC) == SYNTHETIC


def test_parts_carry_fields_and_presence():
    parts = parts_from_meta(SYNTHETIC)
    assert parts.info.title == "T"
    assert parts.info.api_mode == "chat_completions"
    assert parts.info.workspace == "/w"  # запасной вариант — tui.project_path
    assert parts.state.tool_usage_notice == {"fs_read_file": 2}
    assert parts.state.project_memory_check_count == 5
    assert parts.state.previous_response_id is None
    assert "previous_response_id" in parts.state.model_fields_set
    assert parts.runtime.flags == frozenset({"summarizing"})
    assert parts.runtime.image_seq == 4
    assert parts.usage.model_dump()["currency"] == "RUB"
    assert parts.frontend["tui"] == {"status": "idle", "project_path": "/w"}
    assert parts.extra["swarm_meta"] == {"id": 1}


def test_absent_keys_stay_absent():
    meta = {"system": "cli", "thread_id": "t", "messages_count": 1}
    assert _roundtrip(meta) == meta


def test_changed_field_lands_in_develop_key():
    parts = parts_from_meta(SYNTHETIC)
    state = parts.state.model_copy(update={"todo_reminder_count": 7})
    runtime = parts.runtime.model_copy(update={"flags": frozenset()})
    out = meta_from_parts(
        parts.with_(state=state, runtime=runtime), base=SYNTHETIC
    )
    assert out["todo_reminder_cnt"] == 7
    assert "summarizing" not in out["runtime"], "снятый флаг убирается"
    assert out["runtime"]["custom_runtime"] == 1


def test_new_workspace_key_is_written_without_touching_project_path():
    parts = parts_from_meta(SYNTHETIC)
    info = parts.info.model_copy(update={"workspace": "/other"})
    out = meta_from_parts(parts.with_(info=info), base=SYNTHETIC)
    assert out["workspace"] == "/other"
    assert out["tui"]["project_path"] == "/w"


def test_invalid_legacy_value_passes_through():
    meta = {"system": "cli", "thread_id": "t", "user_turn_seq": "три"}
    parts = parts_from_meta(meta)
    assert "user_turn_seq" not in parts.state.model_fields_set
    assert _roundtrip(meta) == meta


@pytest.mark.parametrize("value", [True, "3", 3.0])
def test_coercible_legacy_value_is_not_coerced(value):
    """Не-число в числовом поле develop не становится числом: ``1 == True``,
    поэтому сравнение — по JSON."""
    meta = {
        "system": "cli",
        "thread_id": "t",
        "last_main_prompt_tokens": value,
    }
    parts = parts_from_meta(meta)
    assert "last_prompt_tokens" not in parts.state.model_fields_set
    assert json.dumps(_roundtrip(meta)) == json.dumps(meta)


def test_empty_runtime_section_survives():
    meta = {"system": "cli", "thread_id": "t", "runtime": {}}
    assert _roundtrip(meta) == meta


def test_valid_field_overrides_rejected_legacy_value():
    meta = {"system": "cli", "thread_id": "t", "user_turn_seq": "три"}
    parts = parts_from_meta(meta)
    state = parts.state.model_copy(update={"user_turn_seq": 3})
    assert (
        meta_from_parts(parts.with_(state=state), base=meta)["user_turn_seq"]
        == 3
    )


def test_todo_items_with_extra_fields_stay_in_state():
    parts = parts_from_meta(SYNTHETIC)
    assert parts.state.todo == SYNTHETIC["shared"]["session_todo"]
