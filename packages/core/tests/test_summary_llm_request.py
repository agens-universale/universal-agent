from types import SimpleNamespace

import pytest

from gc_core.summarize import llm as summary_llm
from gc_core.utils import llm_chat_client


def test_auxiliary_llm_completion_kwargs_matches_chat_completions_llm():
    ctx = SimpleNamespace(
        chat_completions_llm=SimpleNamespace(
            completion_kwargs={"reasoning_effort": "none"},
        )
    )
    assert summary_llm.auxiliary_llm_completion_kwargs(ctx) == {
        "reasoning_effort": "none"
    }


def test_auxiliary_llm_judge_max_tokens_from_chat_completions_llm():
    ctx = SimpleNamespace(
        chat_completions_llm=SimpleNamespace(judge_max_tokens=12345)
    )
    assert summary_llm.auxiliary_llm_judge_max_tokens(ctx) == 12345


def test_auxiliary_llm_judge_max_tokens_falls_back_to_default():
    from gc_core.config_modules.chat_completions import DEFAULT_JUDGE_MAX_TOKENS

    ctx = SimpleNamespace(chat_completions_llm=SimpleNamespace())
    assert summary_llm.auxiliary_llm_judge_max_tokens(ctx) == DEFAULT_JUDGE_MAX_TOKENS


def test_request_summary_text_uses_ctx_chat_completions_llm(monkeypatch):
    captured = {}

    async def _fake_request(*, max_tokens=None, payload_overrides=None, **kwargs):
        captured["max_tokens"] = max_tokens
        captured["payload_overrides"] = payload_overrides
        return "<think>x</think>summary ok"

    monkeypatch.setattr(summary_llm, "arequest_chat_completion_text", _fake_request)

    ctx = SimpleNamespace(
        chat_completions_llm=SimpleNamespace(
            max_tokens=7777,
            completion_kwargs={"reasoning_effort": "none"},
        ),
        config_dict={},
        logger=None,
    )
    out = summary_llm.request_summary_text(
        ctx=ctx,
        state=SimpleNamespace(default_model="m"),
        messages=[{"role": "user", "content": "hi"}],
        request_label="summary_batch_1",
        consumer="summary",
    )

    assert out == "summary ok"
    assert captured["max_tokens"] == 7777
    assert captured["payload_overrides"] == {"reasoning_effort": "none"}


def test_request_summary_text_merges_optional_payload_overrides(monkeypatch):
    captured = {}

    async def _fake_request(*, payload_overrides=None, **kwargs):
        captured["payload_overrides"] = payload_overrides
        return "ok"

    monkeypatch.setattr(summary_llm, "arequest_chat_completion_text", _fake_request)

    ctx = SimpleNamespace(
        chat_completions_llm=SimpleNamespace(
            max_tokens=100,
            completion_kwargs={"reasoning_effort": "none"},
        ),
        config_dict={},
    )
    summary_llm.request_summary_text(
        ctx=ctx,
        state=SimpleNamespace(default_model="m"),
        messages=[{"role": "user", "content": "hi"}],
        request_label="summary_batch_1",
        consumer="summary",
        payload_overrides={"temperature": 0.2},
    )
    assert captured["payload_overrides"] == {
        "reasoning_effort": "none",
        "temperature": 0.2,
    }


def test_request_summary_text_requires_chat_completions_llm_on_ctx():
    with pytest.raises(RuntimeError, match="chat_completions_llm"):
        summary_llm.request_summary_text(
            ctx=SimpleNamespace(config_dict={}),
            state=SimpleNamespace(default_model="m"),
            messages=[{"role": "user", "content": "hi"}],
            request_label="summary_batch_1",
            consumer="summary",
        )


def test_apply_payload_overrides_deep_merges_nested_dicts():
    payload = {
        "temperature": 1.0,
        "chat_template_kwargs": {"enable_thinking": True, "keep": 1},
    }
    llm_chat_client.apply_payload_overrides(
        payload,
        {
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    assert payload["reasoning_effort"] == "none"
    assert payload["temperature"] == 1.0
    assert payload["chat_template_kwargs"] == {
        "enable_thinking": False,
        "keep": 1,
    }


def test_request_chat_completion_text_merges_payload_overrides_after_global_kwargs(
    monkeypatch,
):
    captured = {}

    async def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        captured["payload"] = payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "ok"}}]}, {}

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        _fake_policy,
    )
    monkeypatch.setattr(
        llm_chat_client, "create_call_headers", lambda _s, _c, resolved=None: {}
    )

    ctx = SimpleNamespace(
        config_dict={
            "completions_url": "http://example.test/v1/chat/completions",
            "backend_request_timeout_sec": 600,
            "chat_completion_kwargs": {
                "temperature": 1.0,
                "separate_reasoning": True,
                "chat_template_kwargs": {"enable_thinking": True},
            },
        },
        logger=None,
    )

    llm_chat_client.request_chat_completion_text(
        ctx=ctx,
        state=SimpleNamespace(default_model="test-model"),
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=321,
        consumer="summary",
        payload_overrides={
            "reasoning_effort": "none",
            "separate_reasoning": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )

    payload = captured["payload"]
    assert payload["reasoning_effort"] == "none"
    assert payload["separate_reasoning"] is False
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
