"""Паритет stream ↔ non-stream: стрим-ошибка (SSE error в 200-стриме) идёт через
ту же адаптивную политику ретраев, что и HTTP-ошибка того же кода."""

from gc_core import request_backend_policy as policy
from gc_core.utils.chat_completions_stream import BackendStreamError
from gc_testkit import run_coro


async def _anoop(*_a, **_k):
    """Пауза политики стала кооперативной (asyncio.sleep)."""
    return None


def test_policy_retries_stream_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(policy.asyncio, "sleep", _anoop)  # без задержки
    calls = {"n": 0}

    async def _fake_request(
        payload,
        headers,
        url,
        verify,
        use_retry,
        logger,
        on_delta,
        cert=None,
        timeout=600,
        config_proxy=None,
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BackendStreamError(
                "Backend stream error: context length exceeded",
                status_code=400,
            )
        return {
            "choices": [
                {"message": {"content": "ok"}, "finish_reason": "stop"}
            ]
        }

    monkeypatch.setattr(
        policy, "arequest_backend_chat_completions", _fake_request
    )

    res, _payload = run_coro(
        policy.arequest_chat_completions_with_policy(
            payload_factory=lambda mt: {"max_tokens": mt},
            initial_max_tokens=8192,
            min_response_tokens=1024,
            headers={},
            url="http://x",
            verify=True,
            use_retry=False,
        )
    )

    assert res["choices"][0]["message"]["content"] == "ok"
    assert (
        calls["n"] == 2
    )  # 1-й BackendStreamError(400) → адаптивный ретрай → успех
