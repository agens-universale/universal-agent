import json
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
from gc_testkit.session_meta import merge_state_into_meta, shared_from_meta
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
    shared = shared_from_meta(meta)
    assert shared is not None
    assert shared.working_memory["state"] == "in progress"
    assert shared.notes_md == "n"


def test_thread_meta_mapper_rejects_legacy_top_level_memory_files():
    meta = {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "memory_files": {"journal": "x"},
    }
    with pytest.raises(StateSchemaError, match="memory_files"):
        shared_from_meta(meta)


def test_thread_meta_mapper_rejects_legacy_shared_memory_files():
    meta = {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "shared": {"memory_files": {"journal": "x"}, "working_memory": {}},
    }
    with pytest.raises(StateSchemaError, match="memory_files"):
        shared_from_meta(meta)


def test_agent_state_with_a_foreign_schema_version_is_refused():
    """Явно чужая версия схемы состояния — отказ при загрузке. Отсутствие
    версии — нет: её ставит хранилище (как и прежний путь через
    ``meta_from_record``), а файловое — штампом при записи."""
    meta = {
        "shared": {"working_memory": {"goal": "x"}},
        "state_schema_version": "v1",
    }
    with pytest.raises(StateSchemaError, match="state_schema_version"):
        shared_from_meta(meta)
    assert shared_from_meta({"shared": {"working_memory": {"goal": "x"}}})


def test_thread_meta_mapper_allows_empty_meta():
    assert shared_from_meta({}) is None


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
    meta = merge_state_into_meta({}, state)
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


def test_execution_snapshot_retains_routing_progress_through_json():
    from gc_core.auto_routing.policy import Decision

    state = _make_state()
    state.current_level = "light"
    state.steps_in_turn = 17
    state.steps_on_current_level = 4
    state.turn_safe_level = "default"
    state.tick_signals = {"loop"}
    decision = Decision("light", "hold", ("loop",), False)
    state._tick_decision = ("auto", True, decision, "light")
    restored = State.from_execution_snapshot(
        json.loads(json.dumps(state.to_execution_snapshot()))
    )
    assert restored.current_level == "light"
    assert restored.steps_in_turn == 17
    assert restored.steps_on_current_level == 4
    assert restored.turn_safe_level == "default"
    assert restored.tick_signals == {"loop"}
    assert restored._tick_decision == ("auto", True, decision, "light")


def test_execution_snapshot_before_routing_fields_restores_defaults():
    from gc_core.auto_routing.state_ops import ROUTING_FIELDS

    state = _make_state()
    payload = state.to_execution_snapshot()
    for name in (*ROUTING_FIELDS, "turn_messages_start"):
        payload["data"].pop(name, None)
    restored = State.from_execution_snapshot(payload)
    assert restored.current_level is None and restored.tick_signals == set()
    assert restored.turn_messages_start is None
    restored.mark_turn_start()
    assert restored.turn_messages_start == len(restored.messages)
