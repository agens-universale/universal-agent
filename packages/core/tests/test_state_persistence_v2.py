import json
import tempfile
from types import SimpleNamespace

import pytest
from gc_core.intent_log import (
    ACTION_USER_ASK,
    deserialize_list,
    serialize_for_meta,
)
from gc_core.persistence.schema import (
    STATE_SCHEMA_VERSION,
    StateSchemaError,
    meta_has_persisted_agent_state,
    validate_thread_meta_for_load,
)
from gc_core.persistence.state_repository import StateRepository
from gc_core.persistence.state_serializer import StateSerializer
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.states import State
from gc_core.tools.session.stored_outputs.tool_call_outputs_store import (
    ToolCallOutputRecord,
    ToolCallOutputsStore,
)


def _make_state() -> State:
    return State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


FIXED_TS = "2026-05-30T11:35:08Z"


def _user_ask_record(content: str, *, turn: int = 1) -> dict:
    return {
        "turn": turn,
        "ts": FIXED_TS,
        "action": ACTION_USER_ASK,
        "tool_name": None,
        "content": content,
    }


def test_state_from_snapshot_roundtrip_preserves_runtime_fields():
    state = _make_state()
    state.shared.working_memory = {
        "goal": "ship",
        "constraints": "no legacy",
        "state": "active",
    }
    state.shared.notes_md = "notes"
    state.shared.fetch_url_cache = {"https://x": "body"}
    state.user_intent_log = deserialize_list([_user_ask_record("closed turn")])
    state.current_user_intent = deserialize_list(
        [_user_ask_record("open turn", turn=2)]
    )
    state.user_turn_seq = 2
    state.project_memory_content_hash = "lm_hash"
    state.tool_usage_notice_cnt = {"session_memory": 3}
    state.messages = [{"role": "user", "content": "hi", "id": "u1"}]
    state.dialog_steps_offset = 7
    state.tool_call_outputs_store = ToolCallOutputsStore(max_entries=5)
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="ref1",
            source_tool="fs_read_file",
            call_id="c1",
            total_chars=4,
            text="data",
        )
    )
    original_id = state.state_id

    payload = StateSerializer.serialize(state)
    restored = StateSerializer.deserialize(json.loads(json.dumps(payload)))

    assert restored.state_id == original_id
    assert restored.shared.working_memory["goal"] == "ship"
    assert restored.shared.notes_md == "notes"
    assert restored.shared.fetch_url_cache == {"https://x": "body"}
    assert restored.user_intent_log[0].content == "closed turn"
    assert restored.current_user_intent[0].content == "open turn"
    assert restored.user_turn_seq == 2
    assert restored.project_memory_content_hash == "lm_hash"
    assert restored.tool_usage_notice_cnt == {"session_memory": 3}
    assert restored.messages == [{"role": "user", "content": "hi", "id": "u1"}]
    assert restored.dialog_steps_offset == 7
    assert restored.tool_call_outputs_store.peek("ref1") is not None
    assert restored.tool_call_outputs_store.peek("ref1").text == "data"
    # serialize() pops "logger" from the snapshot; restore must default it back
    # to None rather than leaving state.__dict__ without the key (AttributeError).
    assert restored.logger is None


def test_load_state_from_snapshot_defaults_logger_to_none():
    state = _make_state()
    state.logger = SimpleNamespace(warning=lambda *a, **kw: None)

    payload = StateSerializer.serialize(state)
    assert "logger" not in payload

    restored = StateSerializer.deserialize(json.loads(json.dumps(payload)))
    assert restored.logger is None


def test_state_serializer_roundtrip_has_v2_schema_and_working_memory():
    state = _make_state()
    state.shared.working_memory = {"goal": "ship v2", "knowledge": "fact"}
    state.shared.notes_md = "notes"

    payload = StateSerializer.serialize(state)
    assert payload["state_schema_version"] == STATE_SCHEMA_VERSION
    assert "memory_files" not in payload
    assert payload["shared"]["working_memory"]["goal"] == "ship v2"

    raw_json = json.dumps(payload, ensure_ascii=False)
    restored = StateSerializer.deserialize(json.loads(raw_json))
    assert restored.shared.working_memory["goal"] == "ship v2"
    assert restored.shared.notes_md == "notes"


def test_from_snapshot_rejects_legacy_memory_files_in_snapshot():
    state = _make_state()
    payload = StateSerializer.serialize(state)
    payload["shared"]["memory_files"] = {"journal": "legacy"}

    with pytest.raises(StateSchemaError, match="memory_files"):
        StateSerializer.deserialize(payload)


def test_from_snapshot_rejects_missing_state_schema_version():
    state = _make_state()
    payload = StateSerializer.serialize(state)
    del payload["state_schema_version"]

    with pytest.raises(StateSchemaError, match="state_schema_version"):
        StateSerializer.deserialize(payload)


def test_thread_meta_mapper_shared_from_meta_v2():
    meta = {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "shared": {
            "working_memory": {"state": "in progress"},
            "notes_md": "n",
        },
    }
    shared = ThreadMetaMapper.shared_from_meta(meta)
    assert shared is not None
    assert shared.working_memory["state"] == "in progress"
    assert shared.notes_md == "n"


def test_thread_meta_mapper_rejects_legacy_top_level_memory_files():
    meta = {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "memory_files": {"journal": "x"},
    }
    with pytest.raises(StateSchemaError, match="memory_files"):
        ThreadMetaMapper.shared_from_meta(meta)


def test_thread_meta_mapper_rejects_legacy_shared_memory_files():
    meta = {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "shared": {"memory_files": {"journal": "x"}, "working_memory": {}},
    }
    with pytest.raises(StateSchemaError, match="memory_files"):
        ThreadMetaMapper.shared_from_meta(meta)


def test_thread_meta_mapper_rejects_meta_with_agent_state_but_no_schema_version():
    meta = {
        "shared": {"working_memory": {"goal": "x"}},
    }
    with pytest.raises(StateSchemaError, match="state_schema_version"):
        ThreadMetaMapper.shared_from_meta(meta)


def test_thread_meta_mapper_allows_empty_meta():
    assert ThreadMetaMapper.shared_from_meta({}) is None


def test_meta_has_persisted_agent_state_v2_signals_only():
    assert meta_has_persisted_agent_state(
        {"shared": {"working_memory": {"goal": "x"}}}
    )
    assert meta_has_persisted_agent_state(
        {
            "user_intent_log": serialize_for_meta(
                deserialize_list([_user_ask_record("ship")])
            )
        }
    )
    assert not meta_has_persisted_agent_state({"dialog_steps_offset": 3})
    assert not meta_has_persisted_agent_state(
        {"memory_files": {"journal": "legacy"}}
    )


def test_validate_thread_meta_rejects_legacy_before_agent_state_check():
    with pytest.raises(StateSchemaError, match="memory_files"):
        validate_thread_meta_for_load({"memory_files": {"journal": "x"}})


def test_merge_state_into_meta_writes_v2_shared():
    state = _make_state()
    state.shared.working_memory = {"constraints": "no legacy"}
    state.user_intent_log = deserialize_list([_user_ask_record("done")])
    state.user_turn_seq = 1
    meta = ThreadMetaMapper.merge_state_into_meta({}, state)
    assert meta["state_schema_version"] == STATE_SCHEMA_VERSION
    assert meta["shared"]["working_memory"]["constraints"] == "no legacy"
    assert "memory_files" not in meta["shared"]


def test_state_repository_file_roundtrip():
    state = _make_state()
    state.shared.working_memory = {"knowledge": "persisted"}

    with tempfile.NamedTemporaryFile(
        mode="w+", suffix=".json", delete=False
    ) as tmp:
        path = tmp.name

    try:
        StateRepository.save_snapshot({state.state_id: state}, path)
        loaded = StateRepository.load_snapshot(path)
        assert state.state_id in loaded
        assert (
            loaded[state.state_id].shared.working_memory["knowledge"]
            == "persisted"
        )
    finally:
        import os

        os.unlink(path)


def test_state_serializer_roundtrip_from_state_factory():
    state = _make_state()
    payload = StateSerializer.serialize(state)
    assert payload["state_schema_version"] == STATE_SCHEMA_VERSION


def test_get_thread_memory_registers_only_working_memory():
    from gc_core.message_policy.tool_usage_notice.policy import (
        get_thread_memory_registers_from_shared,
    )

    shared = SimpleNamespace(
        working_memory={"state": "ok"},
    )
    assert get_thread_memory_registers_from_shared(shared) == {"state": "ok"}
