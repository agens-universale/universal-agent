"""Фикстуры: грязный state с <think> + мок LLM, снимающий outbound payload."""

from __future__ import annotations

import copy
import logging
from types import SimpleNamespace

import pytest

from gc_core import processing_responses as pr


DIRTY_THINK = "<think>длинное рассуждение агента</think>"
USER_LITERAL_THINK = "цитата <think>не стрипать</think>"


def dirty_canonical_history() -> list[dict]:
    """Канонический dirty storage: think в assistant, literal think в user."""
    return [
        {
            "id": "u1",
            "type": "message",
            "role": "user",
            "content": USER_LITERAL_THINK,
        },
        {
            "id": "a1",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": f"{DIRTY_THINK}Сделаю tool call.",
                }
            ],
        },
        {
            "id": "fc1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
        },
        {
            "id": "fo1",
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "ok",
        },
        {
            "id": "a2",
            "type": "message",
            "role": "assistant",
            "content": f"{DIRTY_THINK}Готово.",
        },
    ]


def make_ctx_state(*, history: list[dict] | None = None):
    cfg = {
        "completions_url": "http://test-host/v1/chat/completions",
        "stream": False,
        "backend_api_mode": "chat_completions",
        "chat_completions_max_tokens": 256,
        "chat_completions_min_response_tokens": 64,
        "backend_request_timeout_sec": 600,
        "use_ssl": False,
        "use_retry": False,
        "x_cl_id": "test-cl-id",
    }
    ctx = SimpleNamespace(
        config_dict=cfg,
        on_stream_delta=None,
        logger=logging.getLogger("test_projection_integration"),
        auth_connector=None,
        is_tui=False,
    )
    state = SimpleNamespace(
        state_id="proj-int-state",
        x_target=None,
        messages=copy.deepcopy(history if history is not None else dirty_canonical_history()),
        tools=[],
        default_model="test-model",
        usage=None,
    )
    return ctx, state


@pytest.fixture
def capture_outbound(monkeypatch):
    """Мок policy: вызывает payload_factory и сохраняет messages перед «вызовом LLM»."""
    captured: dict = {}

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
        timeout=600,
        config_proxy=None,
    ):
        payload = payload_factory(initial_max_tokens)
        captured["payload"] = payload
        captured["messages"] = payload.get("messages")
        captured["request_label"] = request_label
        return (
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
            payload,
        )

    monkeypatch.setattr(pr, "arequest_chat_completions_with_policy", fake_policy)
    return captured
