from types import SimpleNamespace

from message_policy.tool_usage_notice.apply import apply_tool_usage_notice_before_llm
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
        shared=SharedStateData(working_memory={"state": "note"}),
    )


def test_apply_appends_notice_when_threshold_reached():
    state = _state()
    state.tool_usage_notice_cnt = {"working_memory": 10}
    meta: dict = {}
    ctx = SimpleNamespace(
        config_dict={
            "tool_usage_notice": [
                {
                    "tool_name": "working_memory",
                    "step_messages_count": 10,
                    "notice_message": "You have not used working_memory for some steps.",
                }
            ],
            "stop_tools": [],
        }
    )
    out = apply_tool_usage_notice_before_llm(
        [{"role": "user", "content": "hi", "id": "u1"}],
        state,
        ctx,
        meta,
        step_messages=[{"role": "assistant", "content": "ok", "id": "a1"}],
    )
    assert len(out) == 2
    assert "not used working_memory" in out[1]["content"]
    assert state.tool_usage_notice_cnt.get("working_memory", 0) == 0


def test_apply_no_notice_when_rules_empty():
    state = _state()
    state.tool_usage_notice_cnt = {"working_memory": 100}
    meta: dict = {}
    ctx = SimpleNamespace(config_dict={"stop_tools": []})
    out = apply_tool_usage_notice_before_llm(
        [{"role": "user", "content": "hi", "id": "u1"}],
        state,
        ctx,
        meta,
        step_messages=[],
    )
    assert len(out) == 1
    assert state.tool_usage_notice_cnt.get("working_memory", 0) == 100


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
    from message_policy.tool_usage_notice.state import set_long_memory_check_cnt

    set_long_memory_check_cnt(meta, 2)
    monkeypatch.setattr("long_memory.get_hash", lambda _ctx: "new_hash")

    out = apply_tool_usage_notice_before_llm(
        [{"role": "user", "content": "x", "id": "u1"}],
        state,
        ctx,
        meta,
        step_messages=[],
    )
    assert any("baseline" in m["content"].lower() for m in out if m.get("role") == "user")
