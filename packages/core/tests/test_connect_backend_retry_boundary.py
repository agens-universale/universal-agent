"""Ретрай chat/completions живёт на ПУБЛИЧНОЙ функции, а не на внутреннем хелпере.

Декоратор ``retry_with_backoff_async`` снимает ``use_retry`` с kwargs вызова.
При рефакторинге под Sonar он переехал на выделенный хелпер стрима, которому
``use_retry`` никто не передаёт, — и chat/completions перестали ретраиться
вовсе: ``request_backend_policy`` звал с ``use_retry=True``, а первый же 404
(его develop как раз сделал транзиентным) ронял запрос. Тест держит границу:
с ``use_retry=True`` транзиентная ошибка повторяется, без него — нет.
"""

import httpx
import pytest
from gc_core.utils import aretry
from gc_core.utils import connect_backend as cb
from gc_testkit import run_coro
from transport_fakes import install_transport, sse_body

_OK_BODY = sse_body(
    {
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ]
    },
)


def _flaky_handler(failures: int):
    """Первые ``failures`` запросов — 404, дальше валидный стрим."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= failures:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(
            200, text=_OK_BODY, headers={"content-type": "text/event-stream"}
        )

    return handler


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    async def _instant(_delay):
        return None

    monkeypatch.setattr(aretry.asyncio, "sleep", _instant)


def _call(**kwargs):
    return run_coro(
        cb.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            on_delta=lambda kind, text: None,
            **kwargs,
        )
    )


def test_use_retry_repeats_transient_404(monkeypatch):
    spy = install_transport(monkeypatch, _flaky_handler(failures=1))

    res = _call(use_retry=True)

    assert res["choices"][0]["message"]["content"] == "ok"
    assert len(spy.requests) == 2


def test_without_use_retry_single_attempt(monkeypatch):
    spy = install_transport(monkeypatch, _flaky_handler(failures=1))

    with pytest.raises(Exception):  # тип ошибки транспорта тут не важен
        _call()

    assert len(spy.requests) == 1
