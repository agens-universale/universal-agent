"""
Tests: on_delta is wired from ctx.on_stream_delta into
request_chat_completions_with_policy, gated by cfg["stream"].
"""

import logging
from types import SimpleNamespace

from gc_core import processing_responses as pr


def _patch_policy(monkeypatch):
    seen = {}

    async def fake_policy(
        *,
        payload_factory,
        initial_max_tokens,
        min_response_tokens,
        headers,
        url,
        verify,
        cert=None,
        use_retry,
        retry_embedded_choice_errors=True,
        salvage_reasoning=False,
        logger=None,
        on_delta=None,
        request_label=None,
        runtime_notices_enabled=False,
        notice_url=None,
        timeout=None,
        config_proxy=None,
    ):
        seen["on_delta"] = on_delta
        seen["salvage_reasoning"] = salvage_reasoning
        return (
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "x"},
                        "finish_reason": "stop",
                    }
                ]
            },
            {"max_tokens": initial_max_tokens},
        )

    monkeypatch.setattr(
        pr, "arequest_chat_completions_with_policy", fake_policy
    )
    return seen


def _make_ctx_state_messages(stream: bool):
    cfg = {
        "backend.completions_url": "http://test-host/v1/chat/completions",
        "backend.stream": stream,
        "backend.api_mode": "chat_completions",
        "backend.chat_completions_max_tokens": 256,
        "backend.chat_completions_min_response_tokens": 64,
        "backend.use_ssl": False,
        "backend.use_retry": False,
        "backend.request_timeout_sec": 600,
        "agent.x_cl_id": "test-cl-id",
    }
    ctx = fake_ctx(
        cfg,
        on_stream_delta=None,
        logger=logging.getLogger("test"),
        auth_connector=None,
    )
    state = SimpleNamespace(
        state_id="test-state-id",
        x_target=None,
        messages=[],
        tools=[],
        default_model="test-model",
    )
    messages = [{"role": "user", "content": "hi"}]
    return ctx, state, messages


import pytest
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


@pytest.fixture
def chat_state_ctx():
    return _make_ctx_state_messages


def test_on_delta_passed_when_stream_on(monkeypatch, chat_state_ctx):
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=True)
    cb = lambda k, t: None
    ctx.on_stream_delta = cb
    run_coro(pr._arequest_backend_canonical(messages, state, ctx, resolved=None))
    assert seen["on_delta"] is cb


def test_on_delta_none_when_stream_off(monkeypatch, chat_state_ctx):
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=False)
    ctx.on_stream_delta = lambda k, t: None
    run_coro(pr._arequest_backend_canonical(messages, state, ctx, resolved=None))
    assert seen["on_delta"] is None


def test_on_delta_none_when_stream_on_but_no_callback(
    monkeypatch, chat_state_ctx
):
    """stream=True but ctx.on_stream_delta is None → on_delta must be None (headless mode)."""
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=True)
    ctx.on_stream_delta = None
    run_coro(pr._arequest_backend_canonical(messages, state, ctx, resolved=None))
    assert seen["on_delta"] is None


def test_main_agent_funnel_salvages_reasoning(monkeypatch, chat_state_ctx):
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=False)
    run_coro(pr._arequest_backend_canonical(messages, state, ctx, resolved=None))
    assert seen["salvage_reasoning"] is True


def test_muted_answer_drops_only_the_answer_channel(monkeypatch, chat_state_ctx):
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=True)
    received = []
    ctx.on_stream_delta = lambda kind, text: received.append((kind, text))
    run_coro(pr._arequest_backend_canonical(
        messages, state, ctx, resolved=None, mute_answer=True
    ))
    for delta in (("reset", ""), ("think", "думаю"), ("answer", "текст"), ("tool", '{"path"')):
        seen["on_delta"](*delta)
    assert received == [("reset", ""), ("think", "думаю"), ("tool", '{"path"')]


@pytest.mark.parametrize("stream, has_callback", [(False, True), (True, False)])
def test_muted_answer_never_turns_streaming_on(monkeypatch, chat_state_ctx, stream, has_callback):
    """Приглушение — фильтр поверх приёмника, а не повод стримить: без стрима или
    без приёмника `on_delta` остаётся None."""
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=stream)
    ctx.on_stream_delta = (lambda k, t: None) if has_callback else None
    run_coro(pr._arequest_backend_canonical(
        messages, state, ctx, resolved=None, mute_answer=True
    ))
    assert seen["on_delta"] is None
