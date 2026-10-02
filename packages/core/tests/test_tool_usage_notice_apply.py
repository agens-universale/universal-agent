from types import SimpleNamespace

from gc_core.message_policy.tool_usage_notice.apply import (
    aapply_tool_usage_notice_before_llm,
)
from gc_core.states import SharedStateData, State


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


async def test_apply_appends_notice_when_threshold_reached():
    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 10}
    meta: dict = {}
    ctx = SimpleNamespace(
        config_dict={
            "tool_usage_notice": [
                {
                    "tool_name": "session_memory",
                    "step_messages_count": 10,
                    "notice_message": "You have not used session_memory for some steps.",
                }
            ],
            "stop_tools": [],
        }
    )
    out = await aapply_tool_usage_notice_before_llm(
        [{"role": "user", "content": "hi", "id": "u1"}],
        state,
        ctx,
        meta,
        step_messages=[{"role": "assistant", "content": "ok", "id": "a1"}],
    )
    assert len(out) == 2
    assert "not used session_memory" in out[1]["content"]
    assert state.tool_usage_notice_cnt.get("session_memory", 0) == 0


async def test_apply_no_notice_when_rules_empty():
    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 100}
    meta: dict = {}
    ctx = SimpleNamespace(config_dict={"stop_tools": []})
    out = await aapply_tool_usage_notice_before_llm(
        [{"role": "user", "content": "hi", "id": "u1"}],
        state,
        ctx,
        meta,
        step_messages=[],
    )
    assert len(out) == 1
    assert state.tool_usage_notice_cnt.get("session_memory", 0) == 100


async def test_apply_long_changed_hint_on_k_tick(monkeypatch):
    state = _state()
    state.project_memory_content_hash = "old"
    meta: dict = {}
    ctx = SimpleNamespace(
        config_dict={
            "enable_project_memory_notice": True,
            "project_memory_params": {"check_interval_messages": 2},
            "stop_tools": [],
        }
    )
    from gc_core.message_policy.tool_usage_notice.state import (
        set_project_memory_check_cnt,
    )

    set_project_memory_check_cnt(meta, 2)
    monkeypatch.setattr(
        "gc_core.project_memory.get_hash", lambda _ctx: "new_hash"
    )

    out = await aapply_tool_usage_notice_before_llm(
        [{"role": "user", "content": "x", "id": "u1"}],
        state,
        ctx,
        meta,
        step_messages=[],
    )
    assert any(
        "baseline" in m["content"].lower()
        for m in out
        if m.get("role") == "user"
    )


async def test_apply_long_hint_does_not_reset_tool_usage_counter(monkeypatch):
    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 6}
    state.project_memory_content_hash = "old"
    meta: dict = {}
    ctx = SimpleNamespace(
        config_dict={
            "enable_project_memory_notice": True,
            "project_memory_params": {"check_interval_messages": 2},
            "tool_usage_notice": [
                {
                    "tool_name": "session_memory",
                    "step_messages_count": 8,
                    "notice_message": "You have not used session_memory for some steps.",
                }
            ],
            "stop_tools": [],
        }
    )
    from gc_core.message_policy.tool_usage_notice.state import (
        set_project_memory_check_cnt,
    )

    set_project_memory_check_cnt(meta, 2)
    monkeypatch.setattr(
        "gc_core.project_memory.get_hash", lambda _ctx: "new_hash"
    )

    out = await aapply_tool_usage_notice_before_llm(
        [{"role": "user", "content": "x", "id": "u1"}],
        state,
        ctx,
        meta,
        step_messages=[{"role": "assistant", "content": "ok", "id": "a1"}],
    )
    assert any(
        "project memory" in m["content"].lower()
        for m in out
        if m.get("role") == "user"
    )
    assert state.tool_usage_notice_cnt.get("session_memory", 0) == 7
