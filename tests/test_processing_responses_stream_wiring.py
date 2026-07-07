"""
Tests: on_delta is wired from ctx.on_stream_delta into
request_chat_completions_with_policy, gated by cfg["stream"].
"""
import logging
from types import SimpleNamespace

import processing_responses as pr


def _patch_policy(monkeypatch):
    seen = {}

    def fake_policy(
        *,
        payload_factory,
        initial_max_tokens,
        min_response_tokens,
        headers,
        url,
        verify,
        cert=None,
        use_retry,
        logger=None,
        on_delta=None,
        request_label=None,
        runtime_notices_enabled=False,
        notice_url=None,
    ):
        seen["on_delta"] = on_delta
        return (
            {"choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}]},
            {"max_tokens": initial_max_tokens},
        )

    monkeypatch.setattr(pr, "request_chat_completions_with_policy", fake_policy)
    return seen


def _make_ctx_state_messages(stream: bool):
    cfg = {
        "completions_url": "http://test-host/v1/chat/completions",
        "stream": stream,
        "backend_api_mode": "chat_completions",
        "chat_completions_max_tokens": 256,
        "chat_completions_min_response_tokens": 64,
        "use_ssl": False,
        "use_retry": False,
        "x_cl_id": "test-cl-id",
    }
    ctx = SimpleNamespace(
        config_dict=cfg,
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


@pytest.fixture
def chat_state_ctx():
    return _make_ctx_state_messages


def test_on_delta_passed_when_stream_on(monkeypatch, chat_state_ctx):
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=True)
    cb = lambda k, t: None
    ctx.on_stream_delta = cb
    pr._request_backend_canonical(messages, state, ctx)
    assert seen["on_delta"] is cb


def test_on_delta_none_when_stream_off(monkeypatch, chat_state_ctx):
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=False)
    ctx.on_stream_delta = lambda k, t: None
    pr._request_backend_canonical(messages, state, ctx)
    assert seen["on_delta"] is None


def test_on_delta_none_when_stream_on_but_no_callback(monkeypatch, chat_state_ctx):
    """stream=True but ctx.on_stream_delta is None → on_delta must be None (headless mode)."""
    seen = _patch_policy(monkeypatch)
    ctx, state, messages = chat_state_ctx(stream=True)
    ctx.on_stream_delta = None
    pr._request_backend_canonical(messages, state, ctx)
    assert seen["on_delta"] is None
