from gc_core.intent_log.user_turn import init_subagent_open_turn
from gc_core.message_policy.tool_usage_notice.state import (
    META_MEMORY_NOTICE_CNT,
    META_PROJECT_MEMORY_CHECK_CNT,
    META_PROJECT_MEMORY_CONTENT_HASH,
    META_TOOL_USAGE_NOTICE_CNT,
    get_project_memory_check_cnt,
    hydrate_tool_usage_notice_state,
    persist_tool_usage_notice_state,
    set_project_memory_check_cnt,
)
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


def test_new_state_tool_usage_notice_defaults():
    state = _make_state()
    assert state.project_memory_content_hash is None
    assert state.tool_usage_notice_cnt == {}


def test_hydrate_from_meta_restores_state_fields():
    state = _make_state()
    hydrate_tool_usage_notice_state(
        state,
        {
            META_PROJECT_MEMORY_CONTENT_HASH: "abc123",
            META_TOOL_USAGE_NOTICE_CNT: {"session_memory": 7},
            META_PROJECT_MEMORY_CHECK_CNT: 2,
        },
    )
    assert state.project_memory_content_hash == "abc123"
    assert state.tool_usage_notice_cnt == {"session_memory": 7}


def test_hydrate_maps_legacy_memory_notice_cnt_to_session_memory():
    state = _make_state()
    hydrate_tool_usage_notice_state(
        state,
        {META_MEMORY_NOTICE_CNT: 4},
    )
    assert state.tool_usage_notice_cnt == {"session_memory": 4}


def test_hydrate_rejects_invalid_hash_and_negative_counter():
    state = _make_state()
    hydrate_tool_usage_notice_state(
        state,
        {
            META_PROJECT_MEMORY_CONTENT_HASH: 42,
            META_TOOL_USAGE_NOTICE_CNT: {"session_memory": -3},
        },
    )
    assert state.project_memory_content_hash is None
    assert state.tool_usage_notice_cnt == {"session_memory": 0}


def test_persist_writes_state_fields_to_meta():
    state = _make_state()
    state.project_memory_content_hash = "deadbeef"
    state.tool_usage_notice_cnt = {"session_memory": 11}
    meta: dict = {META_PROJECT_MEMORY_CHECK_CNT: 4}
    persist_tool_usage_notice_state(state, meta)
    assert meta[META_PROJECT_MEMORY_CONTENT_HASH] == "deadbeef"
    assert meta[META_TOOL_USAGE_NOTICE_CNT] == {"session_memory": 11}
    assert meta[META_PROJECT_MEMORY_CHECK_CNT] == 4


def test_init_tool_usage_notice_from_meta_on_state():
    state = _make_state()
    hydrate_tool_usage_notice_state(
        state,
        {
            META_PROJECT_MEMORY_CONTENT_HASH: "h1",
            META_TOOL_USAGE_NOTICE_CNT: {"wm": 5},
        },
    )
    assert state.project_memory_content_hash == "h1"
    assert state.tool_usage_notice_cnt == {"wm": 5}


def test_init_subagent_tool_usage_notice_resets_isolated_counters():
    state = _make_state()
    state.project_memory_content_hash = "parent"
    state.tool_usage_notice_cnt = {"session_memory": 99}
    state.init_subagent_tool_usage_notice()
    assert state.project_memory_content_hash is None
    assert state.tool_usage_notice_cnt == {}


def test_init_subagent_current_user_intent_resets_tool_usage_notice():
    state = _make_state()
    state.project_memory_content_hash = "parent"
    state.tool_usage_notice_cnt = {"session_memory": 12}
    init_subagent_open_turn(state, "do task")
    assert state.project_memory_content_hash is None
    assert state.tool_usage_notice_cnt == {}


def test_meta_only_long_memory_check_cnt():
    meta: dict = {}
    set_project_memory_check_cnt(meta, 5)
    assert get_project_memory_check_cnt(meta) == 5


async def test_thread_session_persist_batch_writes_memory_meta(tmp_path):
    """Счётчики напоминаний хода (их ведёт политика в словаре сессии)
    доходят до meta треда на диске."""
    from gc_core.persistence.session_state import session_from_state
    from gc_core.thread_store import ThreadSession, load_thread_meta
    from gc_core.threads.file_store import FileThreadStore

    threads_dir = tmp_path / "threads"
    session = ThreadSession(
        threads_dir=str(threads_dir),
        system_id="cli",
        thread_id="t_mem",
        store=FileThreadStore(threads_dir),
        history_messages=[],
        known_message_keys=set(),
        persisted_messages_count=0,
        previous_response_id=None,
        backend_api_mode="chat_completions",
    )
    state = _make_state()
    state.project_memory_content_hash = "disk_hash"
    state.tool_usage_notice_cnt = {"session_memory": 6}
    set_project_memory_check_cnt(session.notice, 1)

    await session.apersist_batch(
        [],
        previous_response_id=None,
        session=session_from_state(
            session.state,
            state,
            notice=session.notice,
            rendered_system_prompt=None,
            backend_api_mode="chat_completions",
            previous_response_id=None,
        ),
    )

    saved = load_thread_meta(str(threads_dir), "cli", "t_mem")
    assert saved[META_PROJECT_MEMORY_CONTENT_HASH] == "disk_hash"
    assert saved[META_TOOL_USAGE_NOTICE_CNT] == {"session_memory": 6}
    assert saved[META_PROJECT_MEMORY_CHECK_CNT] == 1
