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
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.states import State


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


def test_get_thread_memory_registers_only_working_memory():
    from gc_core.message_policy.tool_usage_notice.policy import (
        get_thread_memory_registers_from_shared,
    )

    shared = SimpleNamespace(
        working_memory={"state": "ok"},
    )
    assert get_thread_memory_registers_from_shared(shared) == {"state": "ok"}
