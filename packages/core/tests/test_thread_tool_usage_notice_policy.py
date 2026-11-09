import json
from types import SimpleNamespace

from gc_core.config_modules.message_notice import ToolUsageNoticeRule
from gc_core.message_policy.tool_usage_notice.policy import (
    batch_used_tool,
    build_subagent_thread_memory_bootstrap_message,
    collect_tool_usage_notices,
    get_thread_memory_registers_from_shared,
    subagent_thread_memory_bootstrap_text,
    update_tool_usage_notice_after_persist,
)
from gc_core.states import State
from gc_testkit.settings import make_settings


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


def _rule(
    step_count: int = 10, message: str = "unused tool notice"
) -> ToolUsageNoticeRule:
    return ToolUsageNoticeRule(
        tool_name="session_memory",
        step_messages_count=step_count,
        notice_message=message,
    )


def test_tool_usage_notice_increments_without_tool_call():
    state = _state()
    rules = [_rule(10)]
    update_tool_usage_notice_after_persist(
        state, [{"role": "assistant", "content": "ok"}], rules
    )
    assert state.tool_usage_notice_cnt["session_memory"] == 1
    update_tool_usage_notice_after_persist(state, [], rules)
    assert state.tool_usage_notice_cnt["session_memory"] == 2


def test_tool_usage_notice_resets_on_tool_batch():
    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 5}
    rules = [_rule(10)]
    update_tool_usage_notice_after_persist(
        state,
        [
            {
                "type": "function_call",
                "name": "session_memory",
                "arguments": "{}",
            }
        ],
        rules,
    )
    assert state.tool_usage_notice_cnt.get("session_memory", 0) == 0


def test_tool_usage_notice_resets_after_explicit_reset():
    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 10}
    rules = [_rule(10)]
    update_tool_usage_notice_after_persist(
        state, [], rules, reset_tool_names={"session_memory"}
    )
    assert state.tool_usage_notice_cnt.get("session_memory", 0) == 0


def test_tool_usage_notice_resets_only_matching_tool_in_batch():
    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 5, "exec_fetch_url": 3}
    rules = [
        ToolUsageNoticeRule(
            tool_name="session_memory",
            step_messages_count=10,
            notice_message="wm",
        ),
        ToolUsageNoticeRule(
            tool_name="exec_fetch_url",
            step_messages_count=10,
            notice_message="fetch",
        ),
    ]
    update_tool_usage_notice_after_persist(
        state,
        [
            {
                "type": "function_call",
                "name": "session_memory",
                "arguments": "{}",
            }
        ],
        rules,
    )
    assert state.tool_usage_notice_cnt["session_memory"] == 0
    assert state.tool_usage_notice_cnt["exec_fetch_url"] == 4


def test_collect_tool_usage_notice_resets_only_emitted_tool():
    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 10, "exec_fetch_url": 3}
    rules = [
        ToolUsageNoticeRule(
            tool_name="session_memory",
            step_messages_count=10,
            notice_message="wm notice",
        ),
        ToolUsageNoticeRule(
            tool_name="exec_fetch_url",
            step_messages_count=10,
            notice_message="fetch notice",
        ),
    ]
    texts = collect_tool_usage_notices(state, rules)
    assert texts == ["wm notice"]
    assert state.tool_usage_notice_cnt["session_memory"] == 0
    assert state.tool_usage_notice_cnt["exec_fetch_url"] == 3


def test_batch_used_tool_detects_function_call():
    assert batch_used_tool(
        [{"type": "function_call", "name": "session_memory"}], "session_memory"
    )
    assert not batch_used_tool(
        [{"type": "function_call", "name": "fs_read_file"}], "session_memory"
    )


def test_batch_used_tool_detects_function_call_output():
    """Имя тула в батче flush — поле tool JSON-ответа, не name сообщения."""
    output = {
        "type": "function_call_output",
        "output": json.dumps({"tool": "session_memory", "ok": True}),
    }
    assert batch_used_tool([output], "session_memory")
    assert not batch_used_tool([output], "session_todo")

    stored = {
        "type": "function_call_output",
        "output": json.dumps(
            {
                "type": "stored_tool_output",
                "source_tool": "session_memory",
                "ok": True,
            }
        ),
    }
    assert batch_used_tool([stored], "session_memory")

    # Страница чтения хранит source_tool исходного тула, а вызван сам reader.
    page = {
        "type": "function_call_output",
        "output": json.dumps(
            {
                "type": "stored_tool_output",
                "tool": "session_read_stored_tool_output",
                "source_tool": "session_memory",
                "ok": True,
            }
        ),
    }
    assert batch_used_tool([page], "session_read_stored_tool_output")
    assert not batch_used_tool([page], "session_memory")


def test_collect_tool_usage_notices_respects_threshold_and_stop_tools():
    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 10}
    rules = [_rule(10, "You have not used session memory for some steps.")]
    texts = collect_tool_usage_notices(
        state, rules, registers={"state": "note"}
    )
    assert texts == ["You have not used session memory for some steps."]
    assert state.tool_usage_notice_cnt.get("session_memory", 0) == 0


def test_collect_tool_usage_notices_skips_below_threshold():
    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 2}
    rules = [_rule(10)]
    assert collect_tool_usage_notices(state, rules) == []


def test_subagent_bootstrap_text_when_policy_enabled():
    cfg = make_settings(
        {
            "tool_usage_notice": [
                {
                    "tool_name": "session_memory",
                    "step_messages_count": 10,
                    "notice_message": "nudge",
                }
            ]
        }
    )
    text = subagent_thread_memory_bootstrap_text(cfg, {"state": "progress"})
    assert text is not None
    assert "Thread memory" in text
    assert "state" in text


def test_subagent_bootstrap_text_disabled_without_rules():
    assert (
        subagent_thread_memory_bootstrap_text(make_settings(), {"state": "x"})
        is None
    )


def test_build_subagent_bootstrap_empty_registers():
    text = build_subagent_thread_memory_bootstrap_message({})
    assert "no registers yet" in text.lower()


def test_get_thread_memory_registers_reads_working_memory():
    shared = SimpleNamespace(
        working_memory={"state": "progress"},
    )
    assert get_thread_memory_registers_from_shared(shared) == {
        "state": "progress"
    }
