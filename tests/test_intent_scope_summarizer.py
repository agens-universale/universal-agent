from types import SimpleNamespace

import pytest

from intent_scope import summarizer
from summarize.llm import auxiliary_llm_completion_kwargs


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
        summarizer, "request_summary_text", _fake_request_summary_text
    )
    monkeypatch.setattr(summarizer, "log_llm_prompt_and_result", lambda **kwargs: None)

    from config_modules.intent_scope import IntentScopeConfig

    ctx = SimpleNamespace(
        intent_scope=IntentScopeConfig(
            tools=["ask_human"],
            max_summary_chars=500,
            max_tool_output_chars=1000,
        ),
        chat_completions_llm=SimpleNamespace(
            max_tokens=100,
            completion_kwargs={"reasoning_effort": "none"},
        ),
        logger=None,
    )
    state = SimpleNamespace(current_user_intent=["prior line"])

    line = summarizer.summarize_tool_intent_line(
        ctx=ctx,
        state=state,
        tool_name="ask_human",
        tool_arguments='{"question":"q"}',
        tool_output_raw='{"response":"yes"}',
    )

    assert line == "User wants a README section on install."
    assert captured["label"] == "intent_scope:ask_human"
    # Intent scope не передаёт свои overrides — только chat_completions.llm в request_summary_text.
    assert captured.get("payload_overrides") is None


def test_summarize_tool_intent_line_fail_open_on_llm_error(monkeypatch):
    monkeypatch.setattr(
        summarizer,
        "request_summary_text",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(summarizer, "log_llm_prompt_and_result", lambda **kwargs: None)

    from config_modules.intent_scope import IntentScopeConfig

    ctx = SimpleNamespace(
        intent_scope=IntentScopeConfig(tools=["ask_human"]),
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
            tool_name="ask_human",
            tool_arguments="",
            tool_output_raw='{"ok":true}',
        )
        == ""
    )
