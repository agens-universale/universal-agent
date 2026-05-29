import json

from message_policy.memory_messages_notice.state import (
    META_LONG_MEMORY_CHECK_CNT,
    META_LONG_MEMORY_CONTENT_HASH,
    META_MEMORY_NOTICE_CNT,
    META_SUBAGENT_THREAD_MEMORY_ANNOUNCED,
    get_long_memory_check_cnt,
    hydrate_memory_messages_state,
    merge_memory_messages_meta_to_base,
    persist_memory_messages_state,
    set_long_memory_check_cnt,
    set_subagent_thread_memory_announced,
)
from states import State
from thread_store import ThreadSession


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


def test_new_state_memory_messages_defaults():
    state = _make_state()
    assert state.long_memory_content_hash is None
    assert state.memory_notice_cnt == 0


def test_hydrate_from_meta_restores_state_fields():
    state = _make_state()
    hydrate_memory_messages_state(
        state,
        {
            META_LONG_MEMORY_CONTENT_HASH: "abc123",
            META_MEMORY_NOTICE_CNT: 7,
            META_LONG_MEMORY_CHECK_CNT: 2,
        },
    )
    assert state.long_memory_content_hash == "abc123"
    assert state.memory_notice_cnt == 7


def test_hydrate_rejects_invalid_hash_and_negative_counter():
    state = _make_state()
    hydrate_memory_messages_state(
        state,
        {META_LONG_MEMORY_CONTENT_HASH: 42, META_MEMORY_NOTICE_CNT: -3},
    )
    assert state.long_memory_content_hash is None
    assert state.memory_notice_cnt == 0


def test_persist_writes_state_fields_to_meta():
    state = _make_state()
    state.long_memory_content_hash = "deadbeef"
    state.memory_notice_cnt = 11
    meta: dict = {META_LONG_MEMORY_CHECK_CNT: 4}
    persist_memory_messages_state(state, meta)
    assert meta[META_LONG_MEMORY_CONTENT_HASH] == "deadbeef"
    assert meta[META_MEMORY_NOTICE_CNT] == 11
    assert meta[META_LONG_MEMORY_CHECK_CNT] == 4


def test_merge_memory_messages_meta_to_base_preserves_ephemeral_keys():
    base: dict = {"created_at": "t0"}
    session = {
        META_LONG_MEMORY_CONTENT_HASH: None,
        META_MEMORY_NOTICE_CNT: 3,
        META_LONG_MEMORY_CHECK_CNT: 2,
        META_SUBAGENT_THREAD_MEMORY_ANNOUNCED: True,
    }
    merge_memory_messages_meta_to_base(base, session)
    assert base[META_LONG_MEMORY_CONTENT_HASH] is None
    assert base[META_MEMORY_NOTICE_CNT] == 3
    assert base[META_LONG_MEMORY_CHECK_CNT] == 2
    assert base[META_SUBAGENT_THREAD_MEMORY_ANNOUNCED] is True


def test_init_memory_messages_from_meta_on_state():
    state = _make_state()
    state.init_memory_messages_from_meta(
        {META_LONG_MEMORY_CONTENT_HASH: "h1", META_MEMORY_NOTICE_CNT: 5}
    )
    assert state.long_memory_content_hash == "h1"
    assert state.memory_notice_cnt == 5


def test_init_subagent_memory_messages_resets_isolated_counters():
    state = _make_state()
    state.long_memory_content_hash = "parent"
    state.memory_notice_cnt = 99
    state.init_subagent_memory_messages()
    assert state.long_memory_content_hash is None
    assert state.memory_notice_cnt == 0


def test_init_subagent_current_user_intent_resets_memory_messages():
    state = _make_state()
    state.long_memory_content_hash = "parent"
    state.memory_notice_cnt = 12
    state.init_subagent_current_user_intent("do task")
    assert state.long_memory_content_hash is None
    assert state.memory_notice_cnt == 0


def test_state_to_dict_roundtrip_restores_memory_fields():
    state = _make_state()
    state.long_memory_content_hash = "hash_x"
    state.memory_notice_cnt = 8
    from persistence.state_serializer import StateSerializer

    payload = StateSerializer.serialize(state)
    restored = StateSerializer.deserialize(json.loads(json.dumps(payload)))
    assert restored.long_memory_content_hash == "hash_x"
    assert restored.memory_notice_cnt == 8


def test_meta_only_helpers():
    meta: dict = {}
    set_long_memory_check_cnt(meta, 5)
    set_subagent_thread_memory_announced(meta, True)
    assert get_long_memory_check_cnt(meta) == 5
    assert meta[META_SUBAGENT_THREAD_MEMORY_ANNOUNCED] is True


def test_thread_session_persist_batch_writes_memory_meta(tmp_path, monkeypatch):
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(
        "thread_store.resolve_threads_dir",
        lambda _cfg: str(threads_dir),
    )

    session = ThreadSession(
        threads_dir=str(threads_dir),
        system_id="cli",
        thread_id="t_mem",
        history_messages=[],
        known_message_keys=set(),
        persisted_messages_count=0,
        meta={},
        previous_response_id=None,
        backend_api_mode="chat_completions",
    )
    state = _make_state()
    state.long_memory_content_hash = "disk_hash"
    state.memory_notice_cnt = 6
    set_long_memory_check_cnt(session.meta, 1)

    persist_memory_messages_state(state, session.meta)
    session.persist_batch([], previous_response_id=None)

    from thread_store import load_thread_meta

    saved = load_thread_meta(str(threads_dir), "cli", "t_mem")
    assert saved[META_LONG_MEMORY_CONTENT_HASH] == "disk_hash"
    assert saved[META_MEMORY_NOTICE_CNT] == 6
    assert saved[META_LONG_MEMORY_CHECK_CNT] == 1
