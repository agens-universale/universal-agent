from types import SimpleNamespace

from gc_core.config_modules.model_registry import effective_completion_kwargs
from gc_core.intent_scope import summarizer
from gc_testkit import run_coro


def test_auxiliary_section_absent_uses_global_auxiliary_kwargs():
    cfg = {
        "auxiliary_llm_params": {
            "completion_kwargs": {
                "reasoning_effort": "none",
                "separate_reasoning": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        }
    }
    assert effective_completion_kwargs(None, cfg, section="auxiliary") == {
        "reasoning_effort": "none",
        "separate_reasoning": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_summarize_tool_intent_line_uses_auxiliary_llm_completion_kwargs(
    monkeypatch,
):
    captured = {}

    async def _fake_request_summary_text(*, payload_overrides=None, **kwargs):
        captured["payload_overrides"] = payload_overrides
        captured["label"] = kwargs.get("request_label")
        return "User wants a README section on install."

    monkeypatch.setattr(
        summarizer, "arequest_summary_text", _fake_request_summary_text
    )

    from gc_core.config_modules.intent_scope import (
        IntentScopeConfig,
        IntentScopeToolConfig,
    )
    from gc_core.intent_log import append_user_ask

    ctx = SimpleNamespace(
        intent_scope=IntentScopeConfig(
            tools={
                "turn_ask_human": IntentScopeToolConfig(record="aggregated")
            },
            max_summary_chars=500,
            max_tool_output_chars=1000,
        ),
        auxiliary_llm=SimpleNamespace(
            max_tokens=100,
            completion_kwargs={"reasoning_effort": "none"},
        ),
        logger=None,
    )
    state = SimpleNamespace(
        current_user_intent=append_user_ask([], "prior line", user_turn_seq=0)
    )

    line = run_coro(
        summarizer.asummarize_tool_intent_line(
            ctx=ctx,
            state=state,
            tool_name="turn_ask_human",
            tool_arguments='{"question":"q"}',
            tool_output_raw='{"response":"yes"}',
        )
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
    async def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(summarizer, "arequest_summary_text", _boom)

    from gc_core.config_modules.intent_scope import (
        IntentScopeConfig,
        IntentScopeToolConfig,
    )

    ctx = SimpleNamespace(
        intent_scope=IntentScopeConfig(
            tools={
                "turn_ask_human": IntentScopeToolConfig(record="aggregated")
            }
        ),
        auxiliary_llm=SimpleNamespace(max_tokens=100, completion_kwargs={}),
        logger=None,
    )
    state = SimpleNamespace(current_user_intent=[])

    assert (
        run_coro(
            summarizer.asummarize_tool_intent_line(
                ctx=ctx,
                state=state,
                tool_name="turn_ask_human",
                tool_arguments="",
                tool_output_raw='{"ok":true}',
            )
        )
        == ""
    )
