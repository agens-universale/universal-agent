from types import SimpleNamespace

from message_policy.long_memory_notice.policy import (
    collect_long_memory_hints,
    long_memory_hints_enabled,
    on_persist_steps,
    touch_long_memory_baseline,
)
from message_policy.memory_messages_notice.state import get_long_memory_check_cnt
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


def test_on_persist_steps_increments_meta_counter():
    meta: dict = {}
    assert on_persist_steps(meta, steps=1) == 1
    assert on_persist_steps(meta, steps=2) == 3
    assert get_long_memory_check_cnt(meta) == 3


def test_collect_hints_empty_before_k_tick():
    state = _state()
    meta: dict = {}
    cfg = {
        "enable_long_memory_notice": True,
        "long_memory_params": {"check_interval_messages": 5},
        "stop_tools": [],
    }
    on_persist_steps(meta, steps=4)
    hints = collect_long_memory_hints(
        state, meta, SimpleNamespace(), cfg, get_hash_fn=lambda _ctx: "abc"
    )
    assert hints == []


def test_collect_exists_hint_on_k_tick_first_hash():
    state = _state()
    meta: dict = {}
    cfg = {
        "enable_long_memory_notice": True,
        "long_memory_params": {"check_interval_messages": 3},
        "stop_tools": [],
    }
    on_persist_steps(meta, steps=3)

    def _hash(_ctx):
        return "hash_v1"

    hints = collect_long_memory_hints(
        state, meta, SimpleNamespace(), cfg, get_hash_fn=_hash
    )
    assert len(hints) == 1
    assert "long memory" in hints[0].lower()
    assert state.long_memory_content_hash == "hash_v1"
    assert get_long_memory_check_cnt(meta) == 0


def test_collect_changed_hint_when_baseline_differs():
    state = _state()
    state.long_memory_content_hash = "old"
    meta: dict = {}
    cfg = {
        "enable_long_memory_notice": True,
        "long_memory_params": {"check_interval_messages": 2},
        "stop_tools": [],
    }
    on_persist_steps(meta, steps=2)
    hints = collect_long_memory_hints(
        state,
        meta,
        SimpleNamespace(),
        cfg,
        get_hash_fn=lambda _ctx: "new",
    )
    assert len(hints) == 1
    assert "baseline" in hints[0].lower()
    assert state.long_memory_content_hash == "old"


def test_touch_long_memory_baseline_updates_state():
    state = _state()
    ctx = SimpleNamespace()
    touch_long_memory_baseline(state, ctx, get_hash_fn=lambda _c: "baseline_x")
    assert state.long_memory_content_hash == "baseline_x"


def test_hints_disabled_when_long_memory_stopped():
    cfg = {
        "enable_long_memory_notice": True,
        "stop_tools": ["long_memory"],
    }
    assert not long_memory_hints_enabled(cfg)
