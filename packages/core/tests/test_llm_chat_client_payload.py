from types import SimpleNamespace

import pytest

from gc_core.utils import llm_chat_client
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx


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
        captured["kwargs"] = kwargs
        return {"choices": [{"message": {"content": "ok"}}]}, payload

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        _fake_policy,
    )
    monkeypatch.setattr(
        llm_chat_client,
        "acreate_call_headers",
        as_coro(lambda _s, _c, resolved=None: {}),
    )
    return captured


def test_request_chat_completion_text_raw_logging_controlled_by_config(
    monkeypatch,
):
    logs = []

    class _Log:
        def info(self, fmt, *args):
            logs.append((fmt, args))

    _patch_policy(monkeypatch)

    state = SimpleNamespace(default_model="m")

    ctx_no_raw = fake_ctx(
        _cfg(),
        logger=_Log(),
    )
    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx_no_raw,
            state=state,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            request_label="summary_batch_1",
            consumer="summary",
        )
    )

    raw_entries = [(f, a) for (f, a) in logs if "text-only raw response" in f]
    assert len(raw_entries) == 0

    ctx_raw = fake_ctx(
        _cfg(logging_params={"full_log_backend": True}),
        logger=_Log(),
    )
    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx_raw,
            state=state,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            request_label="summary_batch_2",
            consumer="summary",
        )
    )

    raw_entries = [(f, a) for (f, a) in logs if "text-only raw response" in f]
    assert len(raw_entries) == 1
    _fmt, args = raw_entries[0]
    assert args[0] == "summary_batch_2"
    assert '"content": "ok"' in args[1]


def test_request_chat_completion_text_uses_max_completion_tokens_and_safe_kwargs(
    monkeypatch,
):
    captured = _patch_policy(monkeypatch)

    ctx = fake_ctx(
        _cfg(
            chat_completion_kwargs={
                "temperature": 0.3,
                "model": "must_not_override",
                "max_tokens": 123,
            },
        ),
        logger=None,
    )
    state = SimpleNamespace(default_model="test-model")

    result = run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=state,
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=321,
            consumer="summary",
        )
    )

    assert result == "ok"
    assert captured["initial_max_tokens"] == 321
    assert captured["payload"]["model"] == "test-model"
    assert captured["payload"]["max_completion_tokens"] == 321
    assert "max_tokens" not in captured["payload"]
    assert captured["payload"]["temperature"] == 0.3


def test_request_chat_completion_text_uses_max_tokens_when_flag_enabled(
    monkeypatch,
):
    captured = _patch_policy(monkeypatch)

    ctx = fake_ctx(
        _cfg(use_max_tokens_param=True),
        logger=None,
    )
    state = SimpleNamespace(default_model="test-model")

    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=state,
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=444,
            consumer="summary",
        )
    )

    assert captured["payload"]["max_tokens"] == 444
    assert "max_completion_tokens" not in captured["payload"]


def test_request_chat_completion_text_omits_empty_tools(monkeypatch):
    # gigacode (/api/v2/chat/completions) отвергает пустой tools:[] 400 Bad Request —
    # этот text-only клиент не использует инструменты, ключ tools не отправляем.
    captured = _patch_policy(monkeypatch)

    ctx = fake_ctx(
        _cfg(),
        logger=None,
    )
    state = SimpleNamespace(default_model="test-model")

    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=state,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            consumer="summary",
        )
    )

    assert "tools" not in captured["payload"]


def test_request_chat_completion_text_does_not_salvage_reasoning(monkeypatch):
    captured = _patch_policy(monkeypatch)
    ctx = fake_ctx(_cfg(), logger=None)
    state = SimpleNamespace(default_model="test-model")
    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=state,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            consumer="summary",
        )
    )
    assert captured["kwargs"]["salvage_reasoning"] is False


_IMAGE_CONTENT = [
    {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + "A" * 400},
    },
    {"type": "text", "text": "что тут?"},
]


def test_service_funnel_rejects_images_without_the_flag(monkeypatch):
    """Служебные вызовы шлют текст (спека §5): картинки без allow_images=True — регрессия."""
    _patch_policy(monkeypatch)
    ctx = fake_ctx(_cfg(), logger=None)
    state = SimpleNamespace(default_model="m")
    with pytest.raises(ValueError):
        run_coro(
            llm_chat_client.arequest_chat_completion_text(
                ctx=ctx,
                state=state,
                messages=[{"role": "user", "content": _IMAGE_CONTENT}],
                max_tokens=10,
                consumer="summary",
            )
        )
    with pytest.raises(ValueError):
        run_coro(
            llm_chat_client.arequest_chat_completion_text(
                ctx=ctx,
                state=state,
                messages=[{"role": "user", "content": "x", "attachments": []}],
                max_tokens=10,
                consumer="summary",
            )
        )


def test_service_funnel_sends_images_with_the_flag(monkeypatch):
    captured = _patch_policy(monkeypatch)
    ctx = fake_ctx(_cfg(), logger=None)
    state = SimpleNamespace(default_model="m")
    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=state,
            messages=[{"role": "user", "content": _IMAGE_CONTENT}],
            max_tokens=10,
            consumer="summary",
            allow_images=True,
        )
    )
    assert captured["payload"]["messages"][0]["content"] == _IMAGE_CONTENT


def test_prompt_log_masks_image_data():
    lines = []

    class _Log:
        def info(self, fmt, *args):
            lines.append(fmt % args)

    llm_chat_client.log_llm_prompt_and_result(
        logger=_Log(),
        label="vision",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": _IMAGE_CONTENT},
        ],
        output_text="кнопка съехала",
    )
    assert all("A" * 400 not in line for line in lines)
    assert any("data:image/png;base64,<400 bytes>" in line for line in lines)
