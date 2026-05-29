import json
import tempfile
from types import SimpleNamespace

import pytest

from persistence.schema import STATE_SCHEMA_VERSION, StateSchemaError
from persistence.state_repository import StateRepository
from persistence.state_serializer import StateSerializer
from persistence.thread_meta_mapper import ThreadMetaMapper
from states import SharedStateData, State


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


def test_deserialize_rejects_legacy_memory_files_in_snapshot():
    state = _make_state()
    payload = StateSerializer.serialize(state)
    payload["shared"]["memory_files"] = {"journal": "legacy"}

    with pytest.raises(StateSchemaError, match="memory_files"):
        StateSerializer.deserialize(payload)


def test_deserialize_rejects_missing_state_schema_version():
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


def test_merge_state_into_meta_writes_v2_shared():
    state = _make_state()
    state.shared.working_memory = {"constraints": "no legacy"}
    state.user_intent_log = ["done"]
    meta = ThreadMetaMapper.merge_state_into_meta({}, state)
    assert meta["state_schema_version"] == STATE_SCHEMA_VERSION
    assert meta["shared"]["working_memory"]["constraints"] == "no legacy"
    assert "memory_files" not in meta["shared"]


def test_state_repository_file_roundtrip():
    state = _make_state()
    state.shared.working_memory = {"knowledge": "persisted"}

    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as tmp:
        path = tmp.name

    try:
        StateRepository.save_snapshot({state.state_id: state}, path)
        loaded = StateRepository.load_snapshot(path)
        assert state.state_id in loaded
        assert loaded[state.state_id].shared.working_memory["knowledge"] == "persisted"
    finally:
        import os

        os.unlink(path)


def test_state_serializer_roundtrip_from_state_factory():
    state = _make_state()
    payload = StateSerializer.serialize(state)
    assert payload["state_schema_version"] == STATE_SCHEMA_VERSION


def test_get_thread_memory_registers_only_working_memory():
    from message_policy.tool_usage_notice.policy import (
        get_thread_memory_registers_from_shared,
    )

    shared = SimpleNamespace(
        working_memory={"state": "ok"},
    )
    assert get_thread_memory_registers_from_shared(shared) == {"state": "ok"}
