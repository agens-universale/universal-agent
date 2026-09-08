"""Пустой SSE-стрим — ретраибельная ошибка, а не «Unknown finish reason».

Провайдер иногда закрывает 200-стрим, не прислав ни content, ни finish_reason.
Молча собранный пустой ответ уходил бы дальше и всплывал загадочной ошибкой
в конвертации; здесь он обязан подняться как BackendStreamError.
"""
import pytest

from gc_core.utils import connect_backend as cb
from gc_core.utils.chat_completions_stream import BackendStreamError

from transport_fakes import install_transport, sse_body, stream_response
from gc_testkit import run_coro


def test_empty_stream_raises_retriable(monkeypatch):
    install_transport(monkeypatch, stream_response(sse_body()))

    with pytest.raises(BackendStreamError) as ei:
        run_coro(cb.arequest_backend_chat_completions(
            {"model": "m", "messages": []}, {}, url="http://x",
            on_delta=lambda kind, text: None,
        ))

    assert ei.value.status_code is None          # None → is_retriable_backend_error True
    assert "empty" in str(ei.value).lower()


def test_nonempty_stream_returns_normally(monkeypatch):
    install_transport(monkeypatch, stream_response(sse_body(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"},
                      "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "!"}, "finish_reason": "stop"}]},
    )))

    res = run_coro(cb.arequest_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x",
        on_delta=lambda kind, text: None,
    ))
    assert res["choices"][0]["message"]["content"] == "hi!"
