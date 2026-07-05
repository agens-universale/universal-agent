import utils.connect_backend as cb


class _Resp:
    def __init__(self): self._sent = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def json(self): return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


def test_no_on_delta_sends_non_stream_payload(monkeypatch):
    captured = {}

    def _fake_post(url, json=None, headers=None, **kw):
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(cb._NO_NETRC_SESSION, "post", _fake_post)
    cb.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x", on_delta=None,
    )
    # без on_delta (CLI/server/swarm) — stream:true в payload НЕ ставится
    assert "stream" not in captured["payload"]
