import request_backend_policy as policy
from utils.chat_completions_stream import BackendStreamError


def test_on_delta_reset_between_retried_attempts(monkeypatch):
    monkeypatch.setattr(policy.time, "sleep", lambda *a, **k: None)
    events = []
    calls = {"n": 0}

    def _fake_request(payload, headers, url, verify, use_retry, logger, on_delta, cert=None, timeout=600):
        calls["n"] += 1
        on_delta("reset", "")               # каждая попытка сбрасывает дисплей
        if calls["n"] == 1:
            on_delta("answer", "частично")   # упавшая попытка успела эмитнуть
            raise BackendStreamError("boom", status_code=500)
        on_delta("answer", "готово")
        return {"choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}]}

    def _on_delta(kind, text):
        events.append((kind, text))

    monkeypatch.setattr(policy, "request_backend_chat_completions", _fake_request)
    res, _ = policy.request_chat_completions_with_policy(
        payload_factory=lambda mt: {"max_tokens": mt},
        initial_max_tokens=8192, min_response_tokens=1024,
        headers={}, url="http://x", verify=True, use_retry=False,
        on_delta=_on_delta,
    )
    assert res["choices"][0]["message"]["content"] == "готово"
    # перед финальным контентом был reset второй попытки → «частично» не «слипается»
    assert ("reset", "") in events
    assert events.index(("reset", "")) < len(events) - 1
    # второй reset идёт ПОСЛЕ первого частичного вывода
    resets = [i for i, e in enumerate(events) if e == ("reset", "")]
    assert len(resets) == 2 and resets[1] > events.index(("answer", "частично"))
