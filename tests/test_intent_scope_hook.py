import json
from types import SimpleNamespace

import pytest

from config_modules.intent_scope import IntentScopeConfig, IntentScopeToolConfig
from intent_log import ACTION_TOOL_AGGREGATED, ACTION_TOOL_RAW, append_user_ask
from intent_scope.append import append_tool_aggregated_to_current, append_tool_raw_to_current
from intent_scope.hook import maybe_update_intent_scope_from_tool
from states import State


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


def _ctx(
    *,
    backend: str,
    tools: dict[str, IntentScopeToolConfig] | None = None,
    fallback: bool = False,
):
    return SimpleNamespace(
        config_dict={"backend_api_mode": backend},
        intent_scope=IntentScopeConfig(
            tools=tools or {},
            fallback_raw_on_llm_error=fallback,
        ),
        logger=None,
    )


def _tool(name: str, *, record: str = "aggregated") -> dict[str, IntentScopeToolConfig]:
    return {name: IntentScopeToolConfig(record=record)}


def test_raw_store_flags_require_at_least_one_enabled():
    with pytest.raises(ValueError, match="requires at least one"):
        IntentScopeToolConfig(record="raw", store_arguments=False, store_output=False)


def test_hook_skipped_on_responses_backend():
    state = _make_state()
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw='{"response":"hi"}',
        state=state,
        ctx=_ctx(backend="responses", tools=_tool("ask_human")),
    )
    assert state.current_user_intent == []


def test_hook_skipped_without_intent_scope_on_ctx():
    state = _make_state()
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw='{"response":"hi"}',
        state=state,
        ctx=SimpleNamespace(
            config_dict={"backend_api_mode": "chat_completions"},
            logger=None,
        ),
    )
    assert state.current_user_intent == []


def test_hook_skipped_when_tools_empty():
    state = _make_state()
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw='{"response":"hi"}',
        state=state,
        ctx=_ctx(backend="chat_completions", tools={}),
    )
    assert state.current_user_intent == []


def test_hook_appends_aggregated_record_after_llm_summary(monkeypatch):
    monkeypatch.setattr(
        "intent_scope.hook.summarize_tool_intent_line",
        lambda **kwargs: "User confirmed option B.",
    )
    state = _make_state()
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw='{"response":"B"}',
        state=state,
        ctx=_ctx(backend="chat_completions", tools=_tool("ask_human")),
    )
    assert len(state.current_user_intent) == 1
    record = state.current_user_intent[0]
    assert record.action == ACTION_TOOL_AGGREGATED
    assert record.tool_name == "ask_human"
    assert record.content == "User confirmed option B."
    assert record.fallback is None


def test_hook_raw_truncates_oversized_output(monkeypatch):
    monkeypatch.setattr(
        "intent_scope.hook.summarize_tool_intent_line",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("LLM must not run for raw")),
    )
    state = _make_state()
    huge = "z" * 20_000
    maybe_update_intent_scope_from_tool(
        tool_name="submit_solution",
        tool_output_raw=json.dumps({"blob": huge}),
        tool_arguments='{"task_report":"done"}',
        state=state,
        ctx=_ctx(
            backend="chat_completions",
            tools=_tool("submit_solution", record="raw"),
        ),
    )
    record = state.current_user_intent[0]
    out_text = record.content["output"]
    assert isinstance(out_text, str)
    assert "[TEXT CUTTED]" in out_text
    assert len(json.dumps(record.content, ensure_ascii=False)) <= 5200


def test_finish_gate_reject_submit_in_current_without_flush():
    """Ветка processing_responses после reject: patch output + hook, без flush."""
    from intent_fixtures import intent_user_ask_texts, seed_state_intent
    from intent_guard.guard import FinishGateDecision
    from intent_guard.submit_solution_emit import (
        apply_finish_gate_to_submit_output_in_state,
        get_function_call_output_text,
    )
    from intent_guard.validation_result import ValidationResult
    from tools.session.submit_solution import SubmitSolution

    state = _make_state()
    seed_state_intent(state, log=["closed turn"], current="open question")
    closed_seq = state.user_turn_seq
    closed_log_len = len(state.user_intent_log)
    flush_calls: list[str] = []
    real_flush = state.flush_current_user_intent

    def _tracked_flush() -> None:
        flush_calls.append("flush")
        real_flush()

    state.flush_current_user_intent = _tracked_flush

    args = json.dumps(
        {
            "solution_type": "full_solution",
            "task_report": "Сделано по запросу пользователя",
            "handoff": "{}",
        },
        ensure_ascii=False,
    )
    raw_ok = SubmitSolution.output_from_cycle_stop_arguments(args)
    assert raw_ok is not None
    state.messages = [
        {
            "type": "function_call",
            "name": "submit_solution",
            "call_id": "call_finish",
            "arguments": args,
        },
        {
            "type": "function_call_output",
            "call_id": "call_finish",
            "output": raw_ok,
        },
    ]
    validation = ValidationResult(
        ok=False,
        action="fix_report_and_continue",
        trace_ok=False,
        report_ok=False,
        trace_gaps=["gap"],
        report_mismatches=["mismatch"],
        missing_requirements=["req"],
        conflicts=["conflict"],
        coverage_score=0.0,
        feedback_for_agent="improve the answer",
    )
    decision = FinishGateDecision(
        outcome="reject",
        validation=validation,
        feedback_text="Finish gate: fix report.",
    )
    apply_finish_gate_to_submit_output_in_state(
        state=state,
        finish_call_id="call_finish",
        decision=decision,
    )
    final_output = get_function_call_output_text(state.messages, "call_finish") or ""
    maybe_update_intent_scope_from_tool(
        tool_name="submit_solution",
        tool_output_raw=final_output,
        tool_arguments=args,
        state=state,
        ctx=_ctx(
            backend="chat_completions",
            tools=_tool("submit_solution", record="raw"),
        ),
    )

    assert flush_calls == []
    assert state.user_turn_seq == closed_seq
    assert len(state.user_intent_log) == closed_log_len
    assert intent_user_ask_texts(state.user_intent_log) == ["closed turn"]
    assert intent_user_ask_texts(state.current_user_intent) == ["open question"]

    submit_records = [
        r
        for r in state.current_user_intent
        if getattr(r, "tool_name", None) == "submit_solution"
    ]
    assert len(submit_records) == 1
    assert submit_records[0].action == ACTION_TOOL_RAW
    output = submit_records[0].content.get("output")
    assert isinstance(output, dict)
    assert output.get("ok") is False
    assert output.get("status") == "rejected_by_finish_gate"
    assert output.get("finish_gate", {}).get("action") == "fix_report_and_continue"


def test_hook_submit_solution_raw_skips_llm_and_stores_object(monkeypatch):
    monkeypatch.setattr(
        "intent_scope.hook.summarize_tool_intent_line",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("LLM must not run for raw")),
    )
    state = _make_state()
    reject_output = (
        '{"ok":false,"rejected_by_finish_gate":true,'
        '"finish_gate":{"feedback":"fix tests"}}'
    )
    maybe_update_intent_scope_from_tool(
        tool_name="submit_solution",
        tool_output_raw=reject_output,
        tool_arguments='{"task_report":"done"}',
        state=state,
        ctx=_ctx(backend="chat_completions", tools=_tool("submit_solution", record="raw")),
    )
    assert len(state.current_user_intent) == 1
    record = state.current_user_intent[0]
    assert record.action == ACTION_TOOL_RAW
    assert record.tool_name == "submit_solution"
    assert record.content["arguments"] == {"task_report": "done"}
    assert record.content["output"]["rejected_by_finish_gate"] is True


def test_hook_submit_solution_raw_can_store_only_output(monkeypatch):
    monkeypatch.setattr(
        "intent_scope.hook.summarize_tool_intent_line",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("LLM must not run for raw")),
    )
    state = _make_state()
    maybe_update_intent_scope_from_tool(
        tool_name="submit_solution",
        tool_output_raw='{"ok":true,"status":"submitted"}',
        tool_arguments='{"task_report":"done"}',
        state=state,
        ctx=_ctx(
            backend="chat_completions",
            tools={
                "submit_solution": IntentScopeToolConfig(
                    record="raw",
                    store_arguments=False,
                    store_output=True,
                )
            },
        ),
    )
    assert len(state.current_user_intent) == 1
    record = state.current_user_intent[0]
    assert record.action == ACTION_TOOL_RAW
    assert "arguments" not in record.content
    assert record.content["output"]["ok"] is True


def test_hook_aggregated_fallback_stores_truncated_content_with_flag(monkeypatch):
    monkeypatch.setattr(
        "intent_scope.hook.summarize_tool_intent_line",
        lambda **kwargs: "",
    )
    state = _make_state()
    raw = '{"ok":false,"error":"validation"}'
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw=raw,
        state=state,
        ctx=_ctx(
            backend="chat_completions",
            tools=_tool("ask_human", record="aggregated"),
            fallback=True,
        ),
    )
    assert len(state.current_user_intent) == 1
    record = state.current_user_intent[0]
    assert record.action == ACTION_TOOL_AGGREGATED
    assert record.fallback is True
    assert record.content == raw


def test_hook_dedup_within_current(monkeypatch):
    monkeypatch.setattr(
        "intent_scope.hook.summarize_tool_intent_line",
        lambda **kwargs: "same line",
    )
    state = _make_state()
    ctx = _ctx(backend="chat_completions", tools=_tool("ask_human"))
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw="a",
        state=state,
        ctx=ctx,
    )
    maybe_update_intent_scope_from_tool(
        tool_name="ask_human",
        tool_output_raw="b",
        state=state,
        ctx=ctx,
    )
    assert len(state.current_user_intent) == 1
    assert state.current_user_intent[0].content == "same line"


def test_hook_turn_close_flush_increments_seq():
    state = _make_state()
    state.current_user_intent = append_user_ask([], "do task", user_turn_seq=0)
    append_tool_aggregated_to_current(state, "ask_human", "picked A")
    append_tool_raw_to_current(
        state,
        "submit_solution",
        {"task_report": "done"},
        output={"ok": True},
    )

    state.flush_current_user_intent()

    assert state.current_user_intent == []
    assert state.user_turn_seq == 1
    assert len(state.user_intent_log) == 3
    assert state.user_intent_log[0].action == "user_ask"
    assert state.user_intent_log[1].action == ACTION_TOOL_AGGREGATED
    assert state.user_intent_log[2].action == ACTION_TOOL_RAW
    assert json.loads(json.dumps(state.user_intent_log[2].content))["output"] == {"ok": True}
