from types import SimpleNamespace

import pytest

from gc_core.intent_scope import summarizer
from gc_core.summarize.llm import auxiliary_llm_completion_kwargs


def test_auxiliary_llm_completion_kwargs_from_chat_completions_llm():
    ctx = SimpleNamespace(
        chat_completions_llm=SimpleNamespace(
            completion_kwargs={
                "reasoning_effort": "none",
                "separate_reasoning": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        )
    )
    assert auxiliary_llm_completion_kwargs(ctx) == {
        "reasoning_effort": "none",
        "separate_reasoning": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_summarize_tool_intent_line_uses_chat_completions_llm_completion_kwargs(monkeypatch):
    captured = {}

    def _fake_request_summary_text(*, payload_overrides=None, **kwargs):
        captured["payload_overrides"] = payload_overrides
        captured["label"] = kwargs.get("request_label")
        return "User wants a README section on install."

    monkeypatch.setattr(
        summarizer, "arequest_summary_text", _fake_request_summary_text
    )

    from gc_core.config_modules.intent_scope import IntentScopeConfig, IntentScopeToolConfig
    from gc_core.intent_log import append_user_ask

    ctx = SimpleNamespace(
        intent_scope=IntentScopeConfig(
            tools={"turn_ask_human": IntentScopeToolConfig(record="aggregated")},
            max_summary_chars=500,
            max_tool_output_chars=1000,
        ),
        chat_completions_llm=SimpleNamespace(
            max_tokens=100,
            completion_kwargs={"reasoning_effort": "none"},
        ),
        logger=None,
    )
    state = SimpleNamespace(
        current_user_intent=append_user_ask([], "prior line", user_turn_seq=0)
    )

    line = summarizer.summarize_tool_intent_line(
        ctx=ctx,
        state=state,
        tool_name="turn_ask_human",
        tool_arguments='{"question":"q"}',
        tool_output_raw='{"response":"yes"}',
    )

    assert line == "User wants a README section on install."
    assert captured["label"] == "intent_scope:turn_ask_human"
    assert captured.get("payload_overrides") is None
    user_msg = summarizer._build_user_prompt(
        tool_name="turn_ask_human",
        tool_arguments='{"question":"q"}',
        tool_output='{"response":"yes"}',
        prior_intent=state.current_user_intent,
        max_tool_output_chars=1000,
    )
    assert "prior line" in user_msg


def test_summarize_tool_intent_line_fail_open_on_llm_error(monkeypatch):
    monkeypatch.setattr(
        summarizer,
        "arequest_summary_text",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    from gc_core.config_modules.intent_scope import IntentScopeConfig, IntentScopeToolConfig

    ctx = SimpleNamespace(
        intent_scope=IntentScopeConfig(
            tools={"turn_ask_human": IntentScopeToolConfig(record="aggregated")}
        ),
        chat_completions_llm=SimpleNamespace(
            max_tokens=100, completion_kwargs={}
        ),
        logger=None,
    )
    state = SimpleNamespace(current_user_intent=[])

    assert (
        summarizer.summarize_tool_intent_line(
            ctx=ctx,
            state=state,
            tool_name="turn_ask_human",
            tool_arguments="",
            tool_output_raw='{"ok":true}',
        )
        == ""
    )
