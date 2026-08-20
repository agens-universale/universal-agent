from gc_core import request_backend_policy as policy


def test_policy_forwards_on_delta(monkeypatch):
    seen = {}

    async def fake_req(payload, headers, *, url, verify, use_retry, logger, on_delta=None, cert=None, timeout=600, config_proxy=None):
        seen["on_delta"] = on_delta
        seen["timeout"] = timeout
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(policy, "arequest_backend_chat_completions", fake_req)

    sentinel = lambda k, t: None
    res, payload = policy.request_chat_completions_with_policy(
        payload_factory=lambda mt: {"model": "m", "max_tokens": mt, "messages": []},
        initial_max_tokens=100,
        min_response_tokens=10,
        headers={},
        url="http://x",
        verify=True,
        use_retry=False,
        on_delta=sentinel,
    )
    assert seen["on_delta"] is sentinel
    assert res["choices"][0]["message"]["content"] == "ok"
