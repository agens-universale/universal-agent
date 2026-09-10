"""Приёмка loop detector: fingerprint, пороги, observe/flush/clear, wiring."""

from __future__ import annotations
from gc_testkit import as_coro

import json
from types import SimpleNamespace

from gc_core.loop_detector.constants import (
    GENERIC_PROFILE,
    LOOP_WARN_ATTR,
    LOOP_WARN_KEY,
    READ_PROFILE,
    WINDOW_SIZE,
    loop_warn_on_repeat,
)
from gc_core.loop_detector.detector import (
    brief_args,
    clear,
    fingerprint,
    flush,
    is_enabled,
    observe,
    resolve_thresholds,
)
from gc_testkit import run_coro
from helpers.turn_finish_apply import stub_turn_finish_apply


def _async_noop(fn):
    """Корутинная заглушка: ядро зовёт диспетчер и хуки только async-именами.

    Синхронную подмену цикл получил бы как готовый результат вместо awaitable
    и упал бы на ``await`` — или, хуже, тихо не исполнил бы её вовсе.
    """
    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)
    return _wrapped



def _state(**extra):
    return SimpleNamespace(
        loop_detector_window=[],
        loop_detector_pending=[],
        **extra,
    )


def _entry(consecutive: int, in_window: int, *, injected=("reason",)):
    @loop_warn_on_repeat(consecutive=consecutive, in_window=in_window)
    class Tool:
        pass

    return {
        "tool_fabric": Tool,
        LOOP_WARN_KEY: {"consecutive": consecutive, "in_window": in_window},
        "injected_fields": list(injected),
        "spec": {},
    }


def _ctx(tools_data, *, enabled=True, inject_tool_fields=None):
    config_dict = {"loop_detector": {"enabled": enabled}}
    if inject_tool_fields is not None:
        config_dict["inject_tool_fields"] = inject_tool_fields
    return SimpleNamespace(
        config_dict=config_dict,
        tools_data=tools_data,
        logger=None,
    )


def test_fingerprint_strips_reason_and_keeps_paging_and_path_literal():
    tools_data = {"fs_read_file": _entry(2, 3)}
    ctx = _ctx(tools_data)
    state = _state()
    observe(
        state,
        ctx,
        "fs_read_file",
        json.dumps({"path": "a.py", "reason": "first look"}),
    )
    observe(
        state,
        ctx,
        "fs_read_file",
        json.dumps({"path": "a.py", "reason": "again"}),
    )
    assert fingerprint("fs_read_file", {"path": "a.py"}) == state.loop_detector_window[0]
    assert state.loop_detector_window[0] == state.loop_detector_window[1]

    state2 = _state()
    observe(state2, ctx, "fs_read_file", json.dumps({"path": "a.py", "offset": 0, "limit": 10}))
    observe(state2, ctx, "fs_read_file", json.dumps({"path": "a.py", "offset": 10, "limit": 10}))
    assert state2.loop_detector_window[0] != state2.loop_detector_window[1]

    assert fingerprint("fs_read_file", {"path": "./a.py"}) != fingerprint(
        "fs_read_file", {"path": "a.py"}
    )
    assert fingerprint("fs_read_file", {"path": "a.py", "offset": None}) != fingerprint(
        "fs_read_file", {"path": "a.py"}
    )


def test_fingerprint_strips_all_injected_fields_even_for_unknown_tools():
    tools_data = {"fs_read_file": _entry(**READ_PROFILE)}
    ctx = _ctx(
        tools_data,
        inject_tool_fields={"reason": "why this call", "why": "extra injected"},
    )
    state = _state()
    observe(
        state,
        ctx,
        "fs_read_file",
        json.dumps({"path": "a.py", "reason": "first", "why": "also first"}),
    )
    observe(
        state,
        ctx,
        "unknown_tool",
        json.dumps({"q": 1, "reason": "mask", "why": "mask2"}),
    )
    observe(
        state,
        ctx,
        "fs_read_file",
        json.dumps({"path": "a.py", "reason": "second", "why": "also second"}),
    )
    assert state.loop_detector_window == [
        fingerprint("fs_read_file", {"path": "a.py"}),
        fingerprint("unknown_tool", {"q": 1}),
        fingerprint("fs_read_file", {"path": "a.py"}),
    ]


def test_brief_args_truncates_each_value_to_100():
    long_text = "x" * 150
    dumped = brief_args({"path": "a.py", "needle": long_text})
    parsed = json.loads(dumped)
    assert parsed["path"] == "a.py"
    assert parsed["needle"] == ("x" * 100) + "..."
    assert len(parsed["needle"]) == 103


def test_read_profile_second_consecutive_hits():
    tools_data = {"fs_read_file": _entry(**READ_PROFILE)}
    ctx = _ctx(tools_data)
    state = _state()
    args = json.dumps({"path": "a.py"})
    observe(state, ctx, "fs_read_file", args)
    assert flush(state) is None
    observe(state, ctx, "fs_read_file", args)
    notice = flush(state)
    assert notice is not None
    assert notice.startswith("Loop detector:")
    assert "fs_read_file" in notice
    assert "a.py" in notice


def test_read_a_other_read_a_silent_third_a_hits_in_window():
    tools_data = {
        "fs_read_file": _entry(**READ_PROFILE),
        "fs_list_files": _entry(**READ_PROFILE),
    }
    ctx = _ctx(tools_data)
    state = _state()
    observe(state, ctx, "fs_read_file", json.dumps({"path": "A"}))
    observe(state, ctx, "fs_list_files", json.dumps({"path": "."}))
    observe(state, ctx, "fs_read_file", json.dumps({"path": "A"}))
    assert flush(state) is None
    observe(state, ctx, "fs_read_file", json.dumps({"path": "A"}))
    notice = flush(state)
    assert notice is not None
    assert "fs_read_file" in notice


def test_unmarked_between_reads_breaks_consecutive():
    tools_data = {
        "fs_read_file": _entry(**READ_PROFILE),
        "fs_write_file": {"tool_fabric": type("W", (), {}), "injected_fields": ["reason"]},
    }
    ctx = _ctx(tools_data)
    state = _state()
    args = json.dumps({"path": "a.py"})
    observe(state, ctx, "fs_read_file", args)
    observe(state, ctx, "fs_write_file", json.dumps({"path": "a.py", "content": "x"}))
    observe(state, ctx, "fs_read_file", args)
    assert flush(state) is None
    assert state.loop_detector_window == [
        fingerprint("fs_read_file", {"path": "a.py"}),
        fingerprint("fs_write_file", {"path": "a.py", "content": "x"}),
        fingerprint("fs_read_file", {"path": "a.py"}),
    ]


def test_unmarked_does_not_clear_in_window_count():
    tools_data = {
        "fs_read_file": _entry(**READ_PROFILE),
        "fs_write_file": {"tool_fabric": type("W", (), {}), "injected_fields": []},
    }
    ctx = _ctx(tools_data)
    state = _state()
    args = json.dumps({"path": "a.py"})
    for _ in range(READ_PROFILE["in_window"] - 1):
        observe(state, ctx, "fs_read_file", args)
        observe(state, ctx, "fs_write_file", json.dumps({"path": "a.py"}))
        assert flush(state) is None
    observe(state, ctx, "fs_read_file", args)
    assert flush(state) is not None


def test_clear_then_same_read_is_silent():
    tools_data = {"fs_read_file": _entry(**READ_PROFILE)}
    ctx = _ctx(tools_data)
    state = _state()
    args = json.dumps({"path": "a.py"})
    observe(state, ctx, "fs_read_file", args)
    observe(state, ctx, "fs_read_file", args)
    assert flush(state) is not None
    clear(state)
    observe(state, ctx, "fs_read_file", args)
    assert flush(state) is None
    assert state.loop_detector_window == [
        fingerprint("fs_read_file", {"path": "a.py"})
    ]


def test_generic_profile_two_quiet_three_or_four_in_window():
    tools_data = {
        "session_think": _entry(**GENERIC_PROFILE),
        "fs_read_file": _entry(**READ_PROFILE),
    }
    ctx = _ctx(tools_data)
    think = json.dumps({"thoughts": "same"})
    state = _state()
    observe(state, ctx, "session_think", think)
    observe(state, ctx, "session_think", think)
    assert flush(state) is None
    observe(state, ctx, "session_think", think)
    assert flush(state) is not None

    state2 = _state()
    for _ in range(3):
        observe(state2, ctx, "session_think", think)
        observe(state2, ctx, "fs_read_file", json.dumps({"path": f"{_}.py"}))
    assert flush(state2) is None
    observe(state2, ctx, "session_think", think)
    assert "session_think" in (flush(state2) or "")


def test_unmarked_tool_never_notices():
    tools_data = {
        "turn_finish": {"tool_fabric": type("F", (), {}), "injected_fields": []},
        "fs_write_file": {"tool_fabric": type("W", (), {}), "injected_fields": []},
        "exec_command": {"tool_fabric": type("E", (), {}), "injected_fields": []},
        "subagent_explore": {"tool_fabric": type("S", (), {}), "injected_fields": []},
    }
    ctx = _ctx(tools_data)
    state = _state()
    for name in tools_data:
        for _ in range(5):
            observe(state, ctx, name, "{}")
    assert flush(state) is None
    assert len(state.loop_detector_window) == WINDOW_SIZE


def test_disabled_config_does_not_record_or_notice():
    tools_data = {"fs_read_file": _entry(**READ_PROFILE)}
    ctx = _ctx(tools_data, enabled=False)
    state = _state()
    args = json.dumps({"path": "a.py"})
    observe(state, ctx, "fs_read_file", args)
    observe(state, ctx, "fs_read_file", args)
    assert flush(state) is None
    assert state.loop_detector_window == []
    assert is_enabled({"loop_detector": {"enabled": False}}) is False
    assert is_enabled({}) is True


def test_flush_one_notice_for_several_hits():
    tools_data = {
        "fs_read_file": _entry(**READ_PROFILE),
        "fs_list_files": _entry(**READ_PROFILE),
    }
    ctx = _ctx(tools_data)
    state = _state()
    observe(state, ctx, "fs_read_file", json.dumps({"path": "a"}))
    observe(state, ctx, "fs_read_file", json.dumps({"path": "a"}))
    observe(state, ctx, "fs_list_files", json.dumps({"path": "."}))
    observe(state, ctx, "fs_list_files", json.dumps({"path": "."}))
    notice = flush(state)
    assert notice is not None
    assert notice.count("Loop detector:") == 1
    assert "fs_read_file" in notice
    assert "fs_list_files" in notice
    assert notice.count("- fs_read_file") == 1
    assert notice.count("- fs_list_files") == 1
    assert flush(state) is None


def test_flush_dedups_identical_hits_in_one_notice():
    tools_data = {"fs_read_file": _entry(**READ_PROFILE)}
    ctx = _ctx(tools_data)
    state = _state()
    args = json.dumps({"path": "a.py"})
    observe(state, ctx, "fs_read_file", args)
    observe(state, ctx, "fs_read_file", args)
    observe(state, ctx, "fs_read_file", args)
    notice = flush(state)
    assert notice is not None
    assert notice.count("Loop detector:") == 1
    assert notice.count("fs_read_file") == 1
    assert "- fs_read_file" not in notice


def test_mcp_flag_on_tools_data_without_decorator():
    tools_data = {
        "mcp_srv__tool": {
            "tool_fabric": lambda **_k: None,
            LOOP_WARN_KEY: dict(GENERIC_PROFILE),
            "injected_fields": ["reason"],
        }
    }
    assert resolve_thresholds(tools_data, "mcp_srv__tool") == (3, 4)
    ctx = _ctx(tools_data)
    state = _state()
    args = json.dumps({"q": 1})
    observe(state, ctx, "mcp_srv__tool", args)
    observe(state, ctx, "mcp_srv__tool", args)
    assert flush(state) is None
    observe(state, ctx, "mcp_srv__tool", args)
    assert flush(state) is not None


def _mcp_registry_entry(server: str, tool: str):
    return {
        "tool": {
            "type": "function",
            "name": f"mcp_{server}_{tool}",
            "description": "x",
            "parameters": {"type": "object", "properties": {}},
        },
        "metadata": {"mcp_server_key": server, "mcp_tool_name": tool},
    }


async def test_mcp_tools_data_has_no_loop_warn_by_default(monkeypatch):
    from gc_core.tools.connect_mcp import registry as reg
    from gc_core.tools.connect_mcp.agent_tools import aget_mcp_tools_data_for_config

    monkeypatch.setattr(
        reg,
        "_abuild_tools_registry",
        as_coro(
            lambda *_a, **_k: (
                {"mcp_srv_search": _mcp_registry_entry("srv", "search")},
                {"srv": {"status": "connected"}},
            )
        ),
    )
    tools_data, _status = await aget_mcp_tools_data_for_config(
        {"mcp-servers": {"srv": {"transport": "stdio", "command": "x"}}}
    )
    assert LOOP_WARN_KEY not in tools_data["mcp_srv_search"]
    assert resolve_thresholds(tools_data, "mcp_srv_search") is None


async def test_mcp_loop_control_tools_all_and_list(monkeypatch):
    from gc_core.tools.connect_mcp import registry as reg
    from gc_core.tools.connect_mcp.agent_tools import aget_mcp_tools_data_for_config

    registry = {
        "mcp_srv_search": _mcp_registry_entry("srv", "search"),
        "mcp_srv_poll": _mcp_registry_entry("srv", "poll"),
    }
    monkeypatch.setattr(
        reg, "_abuild_tools_registry", as_coro(lambda *_a, **_k: (registry, {"srv": {}}))
    )
    listed, _ = await aget_mcp_tools_data_for_config(
        {
            "mcp-servers": {
                "srv": {
                    "transport": "stdio",
                    "command": "x",
                    "loop_control_tools": ["search"],
                }
            }
        }
    )
    assert listed["mcp_srv_search"][LOOP_WARN_KEY] == GENERIC_PROFILE
    assert LOOP_WARN_KEY not in listed["mcp_srv_poll"]

    all_tools, _ = await aget_mcp_tools_data_for_config(
        {
            "mcp-servers": {
                "srv": {
                    "transport": "stdio",
                    "command": "x",
                    "loop_control_tools": "all",
                }
            }
        }
    )
    assert all_tools["mcp_srv_search"][LOOP_WARN_KEY] == GENERIC_PROFILE
    assert all_tools["mcp_srv_poll"][LOOP_WARN_KEY] == GENERIC_PROFILE


def test_window_is_a_ring_of_ten():
    tools_data = {"fs_read_file": _entry(**READ_PROFILE)}
    ctx = _ctx(tools_data)
    state = _state()
    for i in range(WINDOW_SIZE + 3):
        observe(state, ctx, "fs_read_file", json.dumps({"path": f"{i}.py"}))
    assert len(state.loop_detector_window) == WINDOW_SIZE


def test_decorator_puts_thresholds_on_class_not_instance_state():
    from gc_core.loop_detector.constants import copy_loop_warn

    thresholds = {"consecutive": 9, "in_window": 7}

    @loop_warn_on_repeat(**thresholds)
    class Sample:
        pass

    assert getattr(Sample, LOOP_WARN_ATTR) == thresholds
    assert copy_loop_warn(Sample) == thresholds
    assert copy_loop_warn(object()) is None


def test_resolve_thresholds_reads_only_tools_data_key():
    @loop_warn_on_repeat(**READ_PROFILE)
    class Tool:
        pass

    tools_data = {"fs_read_file": {"tool_fabric": Tool}}
    assert resolve_thresholds(tools_data, "fs_read_file") is None

    tools_data["fs_read_file"][LOOP_WARN_KEY] = dict(GENERIC_PROFILE)
    assert resolve_thresholds(tools_data, "fs_read_file") == (3, 4)


def test_builtin_registry_copies_loop_warn_key():
    from gc_core.tools.registry import get_builtin_tools_data

    tools = get_builtin_tools_data({})
    assert tools["fs_read_file"][LOOP_WARN_KEY] == READ_PROFILE
    assert tools["session_think"][LOOP_WARN_KEY] == GENERIC_PROFILE
    assert LOOP_WARN_KEY not in tools["fs_write_file"]
    assert LOOP_WARN_KEY not in tools["exec_command"]
    assert LOOP_WARN_KEY not in tools["turn_finish"]


def test_skills_tools_data_copies_loop_warn_key():
    from gc_core.tools.skills.skills_tools import get_skills_tools_data

    tools = get_skills_tools_data(None)
    for name in (
        "skills_list",
        "skills_load",
        "skills_read_reference",
        "skills_read_asset",
    ):
        assert tools[name][LOOP_WARN_KEY] == READ_PROFILE
        assert not hasattr(tools[name]["tool_fabric"], LOOP_WARN_ATTR)
    assert LOOP_WARN_KEY not in tools["skills_run_script"]


def _ptc_state(**extra):
    return SimpleNamespace(
        messages=[],
        steps=1,
        max_steps=50,
        session_compact_applied=False,
        tool_calls=0,
        turn_tool_calls=0,
        loop_detector_window=[],
        loop_detector_pending=[],
        last_unknown_tool_name=None,
        unknown_tool_repeat_count=0,
        last_internal_error_tool_name=None,
        internal_error_repeat_count=0,
        last_batch_unknown_tool_count=0,
        last_batch_invalid_arguments_count=0,
        stop_requested=False,
        **extra,
    )


def _ptc_ctx(tools_data, *, enabled=True):
    return SimpleNamespace(
        config_dict={"loop_detector": {"enabled": enabled}},
        tools_data=tools_data,
        logger=None,
        run_id="run",
        subagent_progress_sink=None,
    )


def _fc(name, call_id, arguments="{}"):
    return {
        "id": f"id_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def test_process_tool_calls_second_read_executes_and_emits_technical(monkeypatch):
    from gc_core import processing_responses as pr

    calls: list[str] = []

    def fake_apply(**kw):
        calls.append(kw["tool_name"])
        return json.dumps({"ok": True, "path": "a.py"})

    monkeypatch.setattr(pr, "aapply_tool", as_coro(fake_apply))
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None))
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    tools_data = {"fs_read_file": _entry(**READ_PROFILE)}
    args = json.dumps({"path": "a.py", "reason": "look"})
    outs = run_coro(pr.aprocess_tool_calls(
        messages=[_fc("fs_read_file", "c1", args), _fc("fs_read_file", "c2", args)],
        state=_ptc_state(),
        ctx=_ptc_ctx(tools_data),
        run_agent_func=None,
    ))
    assert calls == ["fs_read_file", "fs_read_file"]
    assert [o["call_id"] for o in outs if o.get("type") == "function_call_output"] == [
        "c1",
        "c2",
    ]
    tech = [o for o in outs if o.get("role") == "user"]
    assert len(tech) == 1
    assert "Loop detector:" in tech[0]["content"]
    assert "fs_read_file" in tech[0]["content"]


def test_process_tool_calls_user_stop_clears_window(monkeypatch):
    from gc_core import processing_responses as pr

    state = _ptc_state()

    def fake_apply(**_kw):
        state.stop_requested = True
        return json.dumps({"ok": True, "path": "a.py"})

    monkeypatch.setattr(pr, "aapply_tool", as_coro(fake_apply))
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None))
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    tools_data = {"fs_read_file": _entry(**READ_PROFILE)}
    args = json.dumps({"path": "a.py"})
    outs = run_coro(pr.aprocess_tool_calls(
        messages=[_fc("fs_read_file", "c1", args), _fc("fs_read_file", "c2", args)],
        state=state,
        ctx=_ptc_ctx(tools_data),
        run_agent_func=None,
    ))
    assert state.loop_detector_window == []
    assert state.loop_detector_pending == []
    assert not any("Loop detector:" in str(o.get("content", "")) for o in outs)


def test_run_agent_check_stop_clears_window():
    from gc_core.processing_responses import arun_agent
    from gc_core.states import State

    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.loop_detector_window = ["fp"]
    state.loop_detector_pending = [{"tool": "fs_read_file", "args": {}}]
    result = run_coro(arun_agent(
        [{"role": "user", "content": "hi"}],
        state,
        SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "console_trace": False,
                "no_tool_call_max_count": 50,
            },
            logger=None,
            auth_connector=None,
        ),
        check_stop=lambda: True,
    ))
    assert result is None
    assert state.loop_detector_window == []
    assert state.loop_detector_pending == []


def test_process_tool_calls_disabled_has_no_notice(monkeypatch):
    from gc_core import processing_responses as pr

    monkeypatch.setattr(
        pr, "aapply_tool", as_coro(lambda **_kw: json.dumps({"ok": True}))
    )
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None))
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    tools_data = {"fs_read_file": _entry(**READ_PROFILE)}
    args = json.dumps({"path": "a.py"})
    outs = run_coro(pr.aprocess_tool_calls(
        messages=[_fc("fs_read_file", "c1", args), _fc("fs_read_file", "c2", args)],
        state=_ptc_state(),
        ctx=_ptc_ctx(tools_data, enabled=False),
        run_agent_func=None,
    ))
    assert all(o.get("role") != "user" for o in outs)
    assert not any("Loop detector:" in str(o.get("content", "")) for o in outs)


def test_process_tool_calls_unknown_streak_does_not_emit_loop_notice(monkeypatch):
    from gc_core import processing_responses as pr

    monkeypatch.setattr(
        pr,
        "aapply_tool",
        as_coro(
            lambda **kw: json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": "unknown_tool",
                        "message": "No tool named 'phase1' on client.",
                        "requested": kw["tool_name"],
                        "suggestions": [],
                    },
                    "message": "Unknown tool 'phase1'.",
                }
            )
        ),
    )
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None))
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    state = _ptc_state()
    ctx = _ptc_ctx({"fs_read_file": _entry(**READ_PROFILE)})
    messages = [_fc("phase1", "c1")]
    outs = run_coro(pr.aprocess_tool_calls(messages, state, ctx, run_agent_func=None))
    assert state.unknown_tool_repeat_count == 1
    assert not any("Loop detector:" in str(o.get("content", "")) for o in outs)


def test_compaction_clears_loop_window():
    from gc_core.summarize.compaction import arun_summary_compaction
    from unittest.mock import patch
    from gc_core.summarize.working_memory_gateway import WorkingMemoryDeltas

    state = SimpleNamespace(
        thread_id="t",
        messages=[{"role": "user", "content": "x" * 50}],
        user_intent_log=[],
        current_user_intent=[],
        user_turn_seq=0,
        dialog_steps_offset=0,
        previous_response_id=None,
        loop_detector_window=["keep-me"],
        loop_detector_pending=[{"tool": "fs_read_file", "args": {}}],
        shared=SimpleNamespace(
            working_memory={"goal": "", "constraints": "", "state": "", "knowledge": ""},
            notes_md="",
            fetch_url_cache={},
            confirm_rules={},
        ),
    )
    ctx = SimpleNamespace(config_dict={"summary_params": {}}, logger=None)

    def _fake_summarize(**_kwargs):
        return {
            "raw_summary": "<summary>ok</summary>",
            "num_batches": 1,
            "last_dialog_steps": "",
            "memory_append_deltas": WorkingMemoryDeltas(),
            "memory_replace_deltas": WorkingMemoryDeltas(),
            "memory_append_touched": [],
            "memory_replace_touched": [],
            "trace": {"memory_patch": {"executed": False}, "final_output": {}},
        }

    with patch(
        "gc_core.summarize.compaction.asummarize_dialog_via_chat_completions",
        as_coro(_fake_summarize),
    ), patch(
        "gc_core.summarize.compaction.apply_summary_state_patch",
        lambda **_kw: "dialog-body",
    ), patch(
        "gc_core.summarize.compaction.log_compaction_completed"
    ), patch(
        "gc_core.summarize.compaction.log_summary_message_body"
    ), patch(
        "gc_core.summarize.compaction.log_summary_raw_text"
    ), patch(
        "gc_core.summarize.compaction.log_compaction_started"
    ):
        run_coro(arun_summary_compaction(
            ctx=ctx, state=state, trigger_reason="test", is_cli_mode=False
        ))
    assert state.loop_detector_window == []
    assert state.loop_detector_pending == []


def test_accepted_turn_finish_clears_window(monkeypatch):
    from gc_core.intent_guard import guard as intent_guard_guard
    from gc_core.intent_guard.validation_result import ValidationResult
    from gc_core.processing_responses import arun_agent
    from gc_core.states import State
    from gc_core import processing_responses as pr

    monkeypatch.setattr(pr, "aapply_tool", stub_turn_finish_apply)
    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
        lambda **_kw: ValidationResult(
            ok=True,
            action="accept",
            trace_ok=True,
            report_ok=True,
            trace_gaps=[],
            report_mismatches=[],
            missing_requirements=[],
            conflicts=[],
            coverage_score=1.0,
            feedback_for_agent="",
        ),
    )
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"
    state.loop_detector_window = ["fp"]
    state.loop_detector_pending = [{"tool": "fs_read_file", "args": {}}]
    args = {
        "solution_type": "direct_answer",
        "task_report": "done",
        "handoff": json.dumps({}, ensure_ascii=False),
    }
    result = run_coro(arun_agent(
        [
            {
                "type": "function_call",
                "name": "turn_finish",
                "arguments": json.dumps(args, ensure_ascii=False),
                "call_id": "call_finish",
                "id": "fc_finish",
            }
        ],
        state,
        SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "strict_turn_finish_validation": True,
                "finish_gate_max_iterations": 10,
            },
            logger=None,
            auth_connector=None,
        ),
    ))
    assert result is not None
    assert state.loop_detector_window == []
    assert state.loop_detector_pending == []


def test_accepted_text_response_clears_window():
    from gc_core.processing_responses import arun_agent
    from gc_core.states import State

    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"
    state.loop_detector_window = ["fp"]
    state.loop_detector_pending = [{"tool": "fs_read_file", "args": {}}]
    result = run_coro(arun_agent(
        [{"role": "assistant", "type": "message", "content": "готово"}],
        state,
        SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "text_answer_mode": "allow",
                "console_trace": False,
                "no_tool_call_max_count": 50,
            },
            logger=None,
            auth_connector=None,
        ),
    ))
    assert result == "готово"
    assert state.loop_detector_window == []
    assert state.loop_detector_pending == []


def test_pickup_pending_as_new_turn_clears_window(monkeypatch):
    from gc_core import processing_responses as pr

    monkeypatch.setattr(pr, "begin_turn_main_trace", lambda *_a, **_k: None)
    state = SimpleNamespace(
        loop_detector_window=["fp"],
        loop_detector_pending=[{"tool": "fs_read_file", "args": {}}],
    )
    picked = pr._pickup_pending_as_new_turn(
        lambda: [{"role": "user", "content": "следующий ход"}],
        state,
        SimpleNamespace(logger=None),
    )
    assert picked == [{"role": "user", "content": "следующий ход"}]
    assert state.loop_detector_window == []
    assert state.loop_detector_pending == []


def test_empty_pickup_does_not_clear_window(monkeypatch):
    from gc_core import processing_responses as pr

    monkeypatch.setattr(pr, "begin_turn_main_trace", lambda *_a, **_k: None)
    state = SimpleNamespace(
        loop_detector_window=["fp"],
        loop_detector_pending=[{"tool": "fs_read_file", "args": {}}],
    )
    assert pr._pickup_pending_as_new_turn(lambda: [], state, SimpleNamespace(logger=None)) is None
    assert state.loop_detector_window == ["fp"]
    assert state.loop_detector_pending == [{"tool": "fs_read_file", "args": {}}]


def test_finish_gate_reject_keeps_window(monkeypatch):
    from gc_core.intent_guard import guard as intent_guard_guard
    from gc_core import processing_responses as pr
    from gc_core.intent_guard.validation_result import ValidationResult
    from gc_core.states import State

    monkeypatch.setattr(pr, "aapply_tool", stub_turn_finish_apply)
    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
        as_coro(
            lambda **_kw: ValidationResult(
                ok=False,
                action="continue_work",
                trace_ok=False,
                report_ok=True,
                trace_gaps=["gap"],
                report_mismatches=[],
                missing_requirements=[],
                conflicts=[],
                coverage_score=0.1,
                feedback_for_agent="keep going",
            )
        ),
    )
    cleared: list[int] = []
    real_clear = pr.clear_loop_detector

    def _spy_clear(state):
        cleared.append(1)
        return real_clear(state)

    monkeypatch.setattr(pr, "clear_loop_detector", _spy_clear)
    monkeypatch.setattr(
        pr,
        "aprocess_user_ask",
        as_coro(lambda *a, **k: (_ for _ in ()).throw(SystemExit("stop-after-reject"))),
    )

    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"
    state.loop_detector_window = ["fp"]
    ctx = SimpleNamespace(
        config_dict={
            "backend_api_mode": "chat_completions",
            "strict_turn_finish_validation": True,
            "finish_gate_max_iterations": 10,
            "console_trace": False,
            "no_tool_call_max_count": 50,
        },
        logger=None,
        auth_connector=None,
    )
    messages = [
        {
            "type": "function_call",
            "name": "turn_finish",
            "arguments": json.dumps(
                {
                    "solution_type": "direct_answer",
                    "task_report": "done",
                    "handoff": "{}",
                }
            ),
            "call_id": "call_finish",
            "id": "fc_finish",
        }
    ]
    try:
        run_coro(pr.arun_agent(messages, state, ctx))
    except SystemExit:
        pass
    assert cleared == []
    assert state.loop_detector_window == ["fp"]


def test_profiles_hang_on_listed_tools_not_on_write_exec_finish():
    from gc_core.tools.fs.read_file import ReadFile
    from gc_core.tools.fs.list_files import ListFiles
    from gc_core.tools.fs.file_info import FileInfo
    from gc_core.tools.fs.find_in_files import FindInFiles
    from gc_core.tools.fs.find_files_by_name import FindFilesByName
    from gc_core.tools.session.stored_outputs.read_stored_tool_output import ReadStoredToolOutput
    from gc_core.tools.session.read_user_intent_log import ReadUserIntentLog
    from gc_core.tools.session.think import Think
    from gc_core.tools.session.working_memory import WorkingMemory
    from gc_core.tools.session.todo import SessionTodo
    from gc_core.tools.project.project_memory import ProjectMemory
    from gc_core.tools.exec.calculator import Calculator
    try:
        from gc_core.tools.exec.fetch_url import FetchUrl
    except ModuleNotFoundError:
        from gc_core.tools.exec.simple_fetch_url import FetchUrl
    from gc_core.tools.skills.skills_tools import (
        SkillsListTool,
        LoadSkillTool,
        LoadReferenceTool,
        LoadAssetTool,
        RunSkillScriptTool,
    )
    from gc_core.tools.fs.write_to_file_tool import WriteToFile
    from gc_core.tools.turn.submit_solution import SubmitSolution
    from gc_core.tools.turn.ask_human import AskHuman
    from gc_core.tools.exec.execute_command import ExecuteCommand
    from gc_core.tools.session.session_compact import SessionCompact

    for cls in (
        ReadFile,
        ListFiles,
        FileInfo,
        FindInFiles,
        FindFilesByName,
        ReadStoredToolOutput,
        ReadUserIntentLog,
        Calculator,
        FetchUrl,
        SkillsListTool,
        LoadSkillTool,
        LoadReferenceTool,
        LoadAssetTool,
    ):
        assert getattr(cls, LOOP_WARN_ATTR) == READ_PROFILE
    for cls in (Think, WorkingMemory, SessionTodo, ProjectMemory):
        assert getattr(cls, LOOP_WARN_ATTR) == GENERIC_PROFILE
    for cls in (
        WriteToFile,
        SubmitSolution,
        AskHuman,
        ExecuteCommand,
        SessionCompact,
        RunSkillScriptTool,
    ):
        assert getattr(cls, LOOP_WARN_ATTR, None) is None


def test_config_flatten_wires_loop_detector():
    from gc_core.config_modules.agent import AgentConfig, LoopDetectorConfig
    from gc_core.config_modules.root import AppConfig
    from gc_core.config import flatten_app_config

    cd_on = flatten_app_config(AppConfig(agent=AgentConfig()))
    assert cd_on["loop_detector"] == {"enabled": True}

    cd_off = flatten_app_config(
        AppConfig(agent=AgentConfig(loop_detector=LoopDetectorConfig(enabled=False)))
    )
    assert cd_off["loop_detector"] == {"enabled": False}


def test_serializer_drops_loop_window():
    from gc_core.persistence.state_serializer import StateSerializer
    from gc_core.states import State

    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.loop_detector_window = ["fp"]
    state.loop_detector_pending = [{"tool": "x", "args": {}}]
    payload = StateSerializer.serialize(state)
    assert "loop_detector_window" not in payload
    assert "loop_detector_pending" not in payload
    restored = StateSerializer.deserialize(payload)
    assert restored.loop_detector_window == []
    assert restored.loop_detector_pending == []

