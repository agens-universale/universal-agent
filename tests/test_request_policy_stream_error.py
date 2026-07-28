"""Паритет stream ↔ non-stream: стрим-ошибка (SSE error в 200-стриме) идёт через
ту же адаптивную политику ретраев, что и HTTP-ошибка того же кода."""
import request_backend_policy as policy
from utils.chat_completions_stream import BackendStreamError


def test_policy_retries_stream_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(policy.time, "sleep", lambda *_a, **_k: None)  # без задержки
    calls = {"n": 0}

    def _fake_request(payload, headers, url, verify, use_retry, logger, on_delta, cert=None, timeout=600, config_proxy=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BackendStreamError(
                "Backend stream error: context length exceeded", status_code=400
            )
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(policy, "request_backend_chat_completions", _fake_request)

    res, _payload = policy.request_chat_completions_with_policy(
        payload_factory=lambda mt: {"max_tokens": mt},
        initial_max_tokens=8192,
        min_response_tokens=1024,
        headers={},
        url="http://x",
        verify=True,
        use_retry=False,
    )

    assert res["choices"][0]["message"]["content"] == "ok"
    assert calls["n"] == 2   # 1-й BackendStreamError(400) → адаптивный ретрай → успех
