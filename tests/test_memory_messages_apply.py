from types import SimpleNamespace

from message_policy.memory_messages_notice.apply import apply_memory_messages_before_llm
from states import State, SharedStateData


def _state() -> State:
    return State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=100,
        system_prompt_id="v1",
        use_server_subagents=False,
        shared=SharedStateData(memory_files={"journal": "note"}),
    )


def test_apply_appends_gate_when_notice_threshold_reached():
    state = _state()
    state.memory_notice_cnt = 30
    meta: dict = {}
    ctx = SimpleNamespace(
        config_dict={
            "enable_memory_file_notice": True,
            "memory_gate_params": {"cooldown_messages": 30},
            "stop_tools": [],
        }
    )
    out = apply_memory_messages_before_llm(
        [{"role": "user", "content": "hi", "id": "u1"}],
        state,
        ctx,
        meta,
        step_messages=[{"role": "assistant", "content": "ok", "id": "a1"}],
    )
    assert len(out) == 2
    assert "not used memory_files" in out[1]["content"]
    assert state.memory_notice_cnt == 0


def test_apply_no_gate_when_memory_files_disabled_in_config():
    state = _state()
    state.memory_notice_cnt = 100
    meta: dict = {}
    ctx = SimpleNamespace(
        config_dict={
            "memory_gate_params": {"enable_memory_file_notice": False},
            "stop_tools": [],
        }
    )
    out = apply_memory_messages_before_llm(
        [{"role": "user", "content": "hi", "id": "u1"}],
        state,
        ctx,
        meta,
        step_messages=[],
    )
    assert len(out) == 1
    assert state.memory_notice_cnt == 100


def test_apply_long_changed_hint_on_k_tick(monkeypatch):
    state = _state()
    state.long_memory_content_hash = "old"
    meta: dict = {}
    ctx = SimpleNamespace(
        config_dict={
            "enable_long_memory_notice": True,
            "long_memory_params": {"check_interval_messages": 2},
            "stop_tools": [],
        }
    )
    from message_policy.memory_messages_notice.state import set_long_memory_check_cnt

    set_long_memory_check_cnt(meta, 2)
    monkeypatch.setattr("long_memory.get_hash", lambda _ctx: "new_hash")

    out = apply_memory_messages_before_llm(
        [{"role": "user", "content": "x", "id": "u1"}],
        state,
        ctx,
        meta,
        step_messages=[],
    )
    assert any("baseline" in m["content"].lower() for m in out if m.get("role") == "user")
