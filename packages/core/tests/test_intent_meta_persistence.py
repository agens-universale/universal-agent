from gc_core.intent_log import (
    ACTION_TOOL_AGGREGATED,
    ACTION_USER_ASK,
    IntentRecord,
    append_user_ask,
    deserialize_list,
    serialize_for_meta,
)
from gc_core.intent_log.serialize import LEGACY_LOAD_TS
from gc_core.persistence.schema import STATE_SCHEMA_VERSION
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.states import State
from gc_core.thread_store import load_thread_meta, save_thread_meta

FIXED_TS = "2026-05-30T11:35:08Z"


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


def _closed_record(*, turn: int = 1) -> IntentRecord:
    return IntentRecord(
        turn=turn,
        ts=FIXED_TS,
        action=ACTION_USER_ASK,
        tool_name=None,
        content="closed turn",
    )


def _open_record(*, turn: int = 2) -> IntentRecord:
    return IntentRecord(
        turn=turn,
        ts=FIXED_TS,
        action=ACTION_USER_ASK,
        tool_name=None,
        content="open turn",
    )


def test_merge_state_into_meta_writes_structured_intent_and_seq():
    state = _make_state()
    state.user_intent_log = [_closed_record()]
    state.current_user_intent = [_open_record()]
    state.user_turn_seq = 3

    meta = ThreadMetaMapper.merge_state_into_meta({}, state)

    assert meta["state_schema_version"] == STATE_SCHEMA_VERSION
    assert meta["user_turn_seq"] == 3
    assert meta["user_intent_log"] == serialize_for_meta(state.user_intent_log)
    assert meta["current_user_intent"] == serialize_for_meta(
        state.current_user_intent
    )
    assert meta["user_intent_log"][0]["action"] == ACTION_USER_ASK
    assert meta["user_intent_log"][0]["content"] == "closed turn"


def test_state_init_from_structured_meta():
    meta = {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "user_intent_log": serialize_for_meta([_closed_record()]),
        "current_user_intent": serialize_for_meta([_open_record()]),
        "user_turn_seq": 5,
    }
    state = _make_state()
    state.init_user_intent_log(meta["user_intent_log"])
    state.init_current_user_intent(meta["current_user_intent"])
    state.init_user_turn_seq(meta["user_turn_seq"])

    assert len(state.user_intent_log) == 1
    assert state.user_intent_log[0].content == "closed turn"
    assert state.current_user_intent[0].content == "open turn"
    assert state.user_turn_seq == 5


def test_flush_current_user_intent_increments_seq_and_moves_records():
    state = _make_state()
    state.user_turn_seq = 0
    state.current_user_intent = append_user_ask(
        [], "turn one", user_turn_seq=0, ts=FIXED_TS
    )

    state.flush_current_user_intent()

    assert state.current_user_intent == []
    assert len(state.user_intent_log) == 1
    assert state.user_intent_log[0].turn == 1
    assert state.user_turn_seq == 1


def test_legacy_string_intent_meta_loads_as_user_ask():
    state = _make_state()
    state.init_user_intent_log(["legacy string line"])
    assert len(state.user_intent_log) == 1
    assert state.user_intent_log[0].action == ACTION_USER_ASK
    assert state.user_intent_log[0].content == "legacy string line"
    assert state.user_intent_log[0].ts == LEGACY_LOAD_TS


def test_legacy_string_intent_meta_rewrites_on_persist():
    state = _make_state()
    state.init_user_intent_log(["closed line"])
    state.infer_user_turn_seq_if_missing()
    meta = ThreadMetaMapper.merge_state_into_meta({}, state)
    assert meta["user_intent_log"][0]["action"] == ACTION_USER_ASK
    assert isinstance(meta["user_intent_log"][0]["content"], str)
    assert meta["user_turn_seq"] == 1


def test_meta_has_persisted_agent_state_recognizes_structured_intent():
    meta = {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "user_intent_log": serialize_for_meta([_closed_record()]),
    }
    shared = ThreadMetaMapper.shared_from_meta(meta)
    assert shared is None

    state = _make_state()
    state.init_user_intent_log(meta["user_intent_log"])
    merged = ThreadMetaMapper.merge_state_into_meta(meta, state)
    assert merged["user_intent_log"][0]["content"] == "closed turn"


def test_thread_meta_file_roundtrip_intent_wm_seq(tmp_path):
    state = _make_state()
    state.shared.working_memory = {"state": "active", "goal": "finish step 3"}
    state.user_intent_log = [_closed_record()]
    state.current_user_intent = [_open_record(turn=2)]
    state.user_turn_seq = 1

    meta = ThreadMetaMapper.merge_state_into_meta(
        {"created_at": FIXED_TS},
        state,
    )
    save_thread_meta(str(tmp_path), "sys", "thread1", meta)

    loaded = load_thread_meta(str(tmp_path), "sys", "thread1")
    restored = _make_state()
    restored.init_user_intent_log(loaded["user_intent_log"])
    restored.init_current_user_intent(loaded["current_user_intent"])
    restored.init_user_turn_seq(loaded["user_turn_seq"])
    shared = ThreadMetaMapper.shared_from_meta(loaded)
    assert shared is not None
    restored.shared = shared

    assert restored.user_turn_seq == 1
    assert restored.user_intent_log[0].content == "closed turn"
    assert restored.current_user_intent[0].content == "open turn"
    assert restored.shared.working_memory["goal"] == "finish step 3"


def test_deserialize_legacy_string_entries():
    records = deserialize_list(["user question", "[ask_human] summary"])
    assert len(records) == 2
    assert records[0].action == ACTION_USER_ASK
    assert records[1].action == ACTION_TOOL_AGGREGATED
    assert records[1].tool_name == "ask_human"
