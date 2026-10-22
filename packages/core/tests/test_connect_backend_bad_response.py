"""Пустое или не-JSON тело HTTP-200 от модели — понятная ошибка, а не JSONDecodeError.

Прокси на нестримовые chat/completions отдавал HTTP 200 с пустым телом или с
SSE (``stream:false`` проигнорирован). Разбор падал голым
``Expecting value: line 1 column 1 (char 0)``, политика ретраев считала его
транзиентным и ждала 10/20/40/80 с перед тем же исходом. Теперь SSE-тело
собирается как стрим, пустое или не-JSON — ``BackendBadResponseError`` с одним
быстрым повтором.
"""

import json
import logging

import httpx
import pytest
from gc_core.utils import aretry
from gc_core.utils import connect_backend as cb
from gc_core.utils.backend_errors import (
    BackendBadResponseError,
    is_model_unavailable_error,
)
from gc_testkit import run_coro
from transport_fakes import install_transport, sse_body

_CHUNKS = (
    {"id": "x", "choices": [{"index": 0, "delta": {"content": "ok"}}]},
    {
        "id": "x",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    },
)


@pytest.fixture
def sleeps(monkeypatch):
    """Паузы между попытками — считаем, а не ждём."""
    seen: list[float] = []

    async def _instant(delay):
        seen.append(delay)

    monkeypatch.setattr(aretry.asyncio, "sleep", _instant)
    return seen


def _body(text: str, content_type: str = "application/json"):
    return lambda request: httpx.Response(
        200, text=text, headers={"content-type": content_type}
    )


def _chat(**kwargs):
    return run_coro(
        cb.arequest_backend_chat_completions(
            {"model": "m", "messages": []}, {}, url="http://x", **kwargs
        )
    )


def _responses(**kwargs):
    return run_coro(
        cb.arequest_backend_responses(
            {"model": "m", "input": []}, {}, url="http://x", **kwargs
        )
    )


def test_empty_non_stream_body_is_bad_response_retried_once_fast(
    monkeypatch, sleeps
):
    spy = install_transport(monkeypatch, _body(""))

    with pytest.raises(BackendBadResponseError) as info:
        _chat(use_retry=True)

    assert len(spy.requests) == 2, "плохой ответ повторяется ровно один раз"
    assert len(sleeps) == 1
    assert all(delay < 10 for delay in sleeps), sleeps
    exc = info.value
    assert exc.status_code == 200
    assert exc.length == 0
    assert "HTTP 200" in str(exc)
    assert "Expecting value" not in str(exc)


def test_bad_response_without_use_retry_is_single_attempt(monkeypatch, sleeps):
    spy = install_transport(monkeypatch, _body(""))

    with pytest.raises(BackendBadResponseError):
        _chat()

    assert len(spy.requests) == 1
    assert sleeps == []


def test_bad_response_logs_body_head_but_keeps_it_out_of_message(
    monkeypatch, sleeps, caplog
):
    body = "<html>" + "x" * 500 + "</html>"
    install_transport(monkeypatch, _body(body, "text/html"))

    with caplog.at_level(logging.ERROR):
        with pytest.raises(BackendBadResponseError) as info:
            _chat(logger=logging.getLogger("bad-response-test"))

    assert info.value.content_type == "text/html"
    assert info.value.length == len(body)
    assert "<html>" not in str(info.value)
    logged = caplog.text
    assert "<html>xxx" in logged
    assert "text/html" in logged
    assert "x" * 300 not in logged, "в лог — только начало тела"


def test_sse_body_on_non_stream_request_is_assembled(monkeypatch, sleeps):
    """Прокси проигнорировал stream:false — тело собирается как стрим."""
    install_transport(monkeypatch, _body(sse_body(*_CHUNKS), "text/plain"))

    res = _chat(use_retry=True)

    assert res["choices"][0]["message"]["content"] == "ok"
    assert res["choices"][0]["finish_reason"] == "stop"


def test_sse_body_with_leading_comment_is_assembled(monkeypatch, sleeps):
    install_transport(
        monkeypatch, _body(": ping\n\n" + sse_body(*_CHUNKS), "text/plain")
    )

    res = _chat()

    assert res["choices"][0]["message"]["content"] == "ok"


def test_plain_json_body_is_unchanged(monkeypatch, sleeps):
    install_transport(
        monkeypatch,
        _body(json.dumps({"choices": [{"message": {"content": "blk"}}]})),
    )

    assert _chat()["choices"][0]["message"]["content"] == "blk"


_COMPLETED = {
    "type": "response.completed",
    "response": {"id": "r1", "output": []},
}


def test_responses_empty_body_is_bad_response(monkeypatch, sleeps):
    spy = install_transport(monkeypatch, _body("", "text/event-stream"))

    with pytest.raises(BackendBadResponseError) as info:
        _responses(use_retry=True)

    assert info.value.status_code == 200
    assert len(spy.requests) == 2
    assert all(delay < 10 for delay in sleeps), sleeps


def test_responses_tail_ping_comment_is_ignored(monkeypatch, sleeps):
    body = (
        "event: response.completed\n"
        f"data: {json.dumps(_COMPLETED)}\n\n"
        ": ping\n\n"
    )
    install_transport(monkeypatch, _body(body, "text/event-stream"))

    response, _headers = _responses()

    assert response == {"id": "r1", "output": []}


def test_responses_garbage_tail_is_bad_response(monkeypatch, sleeps):
    install_transport(
        monkeypatch, _body("data: {not json\n\n", "text/event-stream")
    )

    with pytest.raises(BackendBadResponseError):
        _responses()


def test_bad_response_policy():
    exc = BackendBadResponseError(
        status_code=200, content_type="", length=0, api_mode="chat"
    )
    # Длинный backoff не помогает: ответ воспроизводимый. Один быстрый повтор
    # делает `_arequest_authorized`, а не декоратор ретраев.
    assert cb._default_should_retry_backend_exception(exc) is False
    # Модель жива — гасить алиас предохранителем не за что.
    assert is_model_unavailable_error(exc) is False


def test_transient_5xx_keeps_long_backoff(monkeypatch, sleeps):
    spy = install_transport(
        monkeypatch, lambda request: httpx.Response(503, text="busy")
    )

    with pytest.raises(httpx.HTTPStatusError):
        _chat(use_retry=True)

    assert len(spy.requests) == 5
    assert sleeps == [10.0, 20.0, 40.0, 80.0]
