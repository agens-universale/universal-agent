from types import SimpleNamespace

import pytest

from intent_scope import summarizer


def test_build_intent_scope_payload_overrides_merges_thinking_off(monkeypatch):
    ctx = SimpleNamespace(
        summary_llm=SimpleNamespace(
            completion_kwargs={
                "temperature": 0.5,
                "separate_reasoning": True,
                "chat_template_kwargs": {"enable_thinking": True, "keep": 1},
            }
        )
    )
    overrides = summarizer.build_intent_scope_payload_overrides(ctx)
    assert overrides["temperature"] == 0.5
    assert overrides["separate_reasoning"] is False
    assert overrides["chat_template_kwargs"] == {
        "enable_thinking": False,
        "keep": 1,
    }


def test_summarize_tool_intent_line_uses_payload_overrides(monkeypatch):
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
        summary_llm=SimpleNamespace(
            max_tokens=100,
            completion_kwargs={"separate_reasoning": True},
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
    assert captured["payload_overrides"]["separate_reasoning"] is False
    assert captured["payload_overrides"]["chat_template_kwargs"] == {
        "enable_thinking": False
    }


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
        summary_llm=SimpleNamespace(
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
