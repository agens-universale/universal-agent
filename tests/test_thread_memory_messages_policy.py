from types import SimpleNamespace

from message_policy.memory_messages_notice.policy import (
    batch_used_memory_files,
    build_memory_gate_message,
    get_thread_memory_registers_from_shared,
    maybe_emit_memory_gate,
    maybe_emit_subagent_wiki_bootstrap,
    preferred_thread_memory_tool_name,
    update_memory_notice_after_persist,
)
from states import State


def _state() -> State:
    return State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=1,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def test_memory_notice_increments_without_memory_files():
    state = _state()
    update_memory_notice_after_persist(state, [{"role": "assistant", "content": "ok"}])
    assert state.memory_notice_cnt == 1
    update_memory_notice_after_persist(state, [])
    assert state.memory_notice_cnt == 2


def test_memory_notice_resets_on_memory_files_batch():
    state = _state()
    state.memory_notice_cnt = 5
    update_memory_notice_after_persist(
        state,
        [{"type": "function_call", "name": "working_memory", "arguments": "{}"}],
    )
    assert state.memory_notice_cnt == 0


def test_memory_notice_resets_after_emitted_notice():
    state = _state()
    state.memory_notice_cnt = 10
    update_memory_notice_after_persist(state, [], emitted_memory_notice=True)
    assert state.memory_notice_cnt == 0


def test_batch_used_memory_files_detects_function_call():
    assert batch_used_memory_files(
        [{"type": "function_call", "name": "working_memory"}]
    )
    assert batch_used_memory_files(
        [{"type": "function_call", "name": "memory_files"}]
    )
    assert not batch_used_memory_files([{"type": "function_call", "name": "read_file"}])


def test_preferred_thread_memory_tool_name_with_stop_tools():
    assert preferred_thread_memory_tool_name({"stop_tools": []}) == "working_memory"
    assert preferred_thread_memory_tool_name({"stop_tools": ["working_memory"]}) == "memory_files"
    assert preferred_thread_memory_tool_name({"stop_tools": ["working_memory", "memory_files"]}) == "working_memory"


def test_maybe_emit_memory_gate_respects_threshold_and_stop_tools():
    state = _state()
    state.memory_notice_cnt = 30
    cfg = {
        "enable_memory_file_notice": True,
        "memory_gate_params": {"cooldown_messages": 30},
        "stop_tools": [],
    }
    text = maybe_emit_memory_gate(state, cfg, {"journal": "note"})
    assert text is not None
    assert "not used working_memory" in text
    assert "journal" in text

    cfg_blocked = {**cfg, "stop_tools": ["working_memory", "memory_files"]}
    assert maybe_emit_memory_gate(state, cfg_blocked, {"journal": "x"}) is None


def test_memory_gate_disabled_when_notice_flag_off():
    state = _state()
    state.memory_notice_cnt = 100
    cfg = {
        "memory_gate_params": {"enable_memory_file_notice": False},
        "stop_tools": [],
    }
    assert maybe_emit_memory_gate(state, cfg, {}) is None


def test_subagent_bootstrap_once():
    meta: dict = {}
    cfg = {"enable_memory_file_notice": True, "stop_tools": []}
    first = maybe_emit_subagent_wiki_bootstrap(meta, cfg, {"journal": "j"})
    second = maybe_emit_subagent_wiki_bootstrap(meta, cfg, {"journal": "j"})
    assert first is not None
    assert "Thread memory (shared wiki)" in first
    assert second is None


def test_build_memory_gate_message_includes_register_index():
    msg = build_memory_gate_message(
        cooldown_messages=30,
        memory_files={"arch": "line1\nline2"},
        tool_name="working_memory",
    )
    assert "30" in msg
    assert "arch" in msg


def test_get_thread_memory_registers_prefers_working_memory():
    shared = SimpleNamespace(
        working_memory={"state": "progress"},
        memory_files={"journal": "legacy"},
    )
    assert get_thread_memory_registers_from_shared(shared) == {"state": "progress"}
