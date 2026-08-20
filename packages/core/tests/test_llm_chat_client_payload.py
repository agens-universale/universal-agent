from types import SimpleNamespace

from gc_core.utils import llm_chat_client


def _cfg(**overrides):
    cfg = {
        "completions_url": "http://example.test/v1/chat/completions",
        "backend_request_timeout_sec": 600,
    }
    cfg.update(overrides)
    return cfg


def _patch_policy(monkeypatch):
    captured = {}

    async def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        payload = payload_factory(initial_max_tokens)
        captured["payload"] = payload
        captured["initial_max_tokens"] = initial_max_tokens
        return {"choices": [{"message": {"content": "ok"}}]}, payload

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        _fake_policy,
    )
    monkeypatch.setattr(llm_chat_client, "create_call_headers", lambda _s, _c: {})
    return captured


def test_request_chat_completion_text_raw_logging_controlled_by_config(monkeypatch):
    logs = []

    class _Log:
        def info(self, fmt, *args):
            logs.append((fmt, args))

    _patch_policy(monkeypatch)

    state = SimpleNamespace(default_model="m")

    ctx_no_raw = SimpleNamespace(
        config_dict=_cfg(),
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
        config_dict=_cfg(logging_params={"full_log_backend": True}),
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
    captured = _patch_policy(monkeypatch)

    ctx = SimpleNamespace(
        config_dict=_cfg(
            chat_completion_kwargs={
                "temperature": 0.3,
                "model": "must_not_override",
                "max_tokens": 123,
            },
        ),
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
    assert captured["initial_max_tokens"] == 321
    assert captured["payload"]["model"] == "test-model"
    assert captured["payload"]["max_completion_tokens"] == 321
    assert "max_tokens" not in captured["payload"]
    assert captured["payload"]["temperature"] == 0.3


def test_request_chat_completion_text_uses_max_tokens_when_flag_enabled(monkeypatch):
    captured = _patch_policy(monkeypatch)

    ctx = SimpleNamespace(
        config_dict=_cfg(use_max_tokens_param=True),
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


def test_request_chat_completion_text_omits_empty_tools(monkeypatch):
    # gigacode (/api/v2/chat/completions) отвергает пустой tools:[] 400 Bad Request —
    # этот text-only клиент не использует инструменты, ключ tools не отправляем.
    captured = _patch_policy(monkeypatch)

    ctx = SimpleNamespace(
        config_dict=_cfg(),
        logger=None,
    )
    state = SimpleNamespace(default_model="test-model")

    llm_chat_client.request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
    )

    assert "tools" not in captured["payload"]
