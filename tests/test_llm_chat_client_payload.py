from types import SimpleNamespace

from utils import llm_chat_client


def test_request_chat_completion_text_raw_logging_controlled_by_config(monkeypatch):
    logs = []

    class _Log:
        def info(self, fmt, *args):
            logs.append((fmt, args))

    def _fake_headers(_state, _ctx):
        return {}

    def _fake_request(*_a, **_k):
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(llm_chat_client, "create_call_headers", _fake_headers)
    monkeypatch.setattr(
        llm_chat_client,
        "request_backend_chat_completions",
        _fake_request,
    )

    state = SimpleNamespace(default_model="m")

    ctx_no_raw = SimpleNamespace(
        config_dict={"completions_url": "http://example.test/v1/chat/completions"},
        logger=_Log(),
    )
    llm_chat_client.request_chat_completion_text(
        ctx=ctx_no_raw,
        state=state,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
        request_label="summary_batch_1",
    )

    raw_entries = [(f, a) for (f, a) in logs if "text-only raw response" in f]
    assert len(raw_entries) == 0

    ctx_raw = SimpleNamespace(
        config_dict={
            "completions_url": "http://example.test/v1/chat/completions",
            "logging_params": {"full_log_backend": True},
        },
        logger=_Log(),
    )
    llm_chat_client.request_chat_completion_text(
        ctx=ctx_raw,
        state=state,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
        request_label="summary_batch_2",
    )

    raw_entries = [(f, a) for (f, a) in logs if "text-only raw response" in f]
    assert len(raw_entries) == 1
    _fmt, args = raw_entries[0]
    assert args[0] == "summary_batch_2"
    assert '"content": "ok"' in args[1]


def test_request_chat_completion_text_uses_max_completion_tokens_and_safe_kwargs(monkeypatch):
    captured = {}

    def _fake_headers(_state, _ctx):
        return {"Authorization": "Bearer fake"}

    def _fake_request(payload, headers, **kwargs):
        captured["payload"] = payload
        captured["headers"] = headers
        captured["kwargs"] = kwargs
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(llm_chat_client, "create_call_headers", _fake_headers)
    monkeypatch.setattr(
        llm_chat_client,
        "request_backend_chat_completions",
        _fake_request,
    )

    ctx = SimpleNamespace(
        config_dict={
            "completions_url": "http://example.test/v1/chat/completions",
            "chat_completion_kwargs": {
                "temperature": 0.3,
                "model": "must_not_override",
                "max_tokens": 123,
            },
        },
        logger=None,
    )
    state = SimpleNamespace(default_model="test-model")

    result = llm_chat_client.request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=321,
    )

    assert result == "ok"
    assert captured["payload"]["model"] == "test-model"
    assert captured["payload"]["max_completion_tokens"] == 321
    assert "max_tokens" not in captured["payload"]
    assert captured["payload"]["temperature"] == 0.3


def test_request_chat_completion_text_uses_max_tokens_when_flag_enabled(monkeypatch):
    captured = {}

    def _fake_headers(_state, _ctx):
        return {"Authorization": "Bearer fake"}

    def _fake_request(payload, headers, **kwargs):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(llm_chat_client, "create_call_headers", _fake_headers)
    monkeypatch.setattr(
        llm_chat_client,
        "request_backend_chat_completions",
        _fake_request,
    )

    ctx = SimpleNamespace(
        config_dict={
            "completions_url": "http://example.test/v1/chat/completions",
            "use_max_tokens_param": True,
        },
        logger=None,
    )
    state = SimpleNamespace(default_model="test-model")

    llm_chat_client.request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=444,
    )

    assert captured["payload"]["max_tokens"] == 444
    assert "max_completion_tokens" not in captured["payload"]
