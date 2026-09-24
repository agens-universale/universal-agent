"""_request_backend_canonical: на 401 (истёкший токен) пере-выпускает токен и
повторяет запрос ровно один раз; прочие ошибки — наверх без ретрая."""

import types

import httpx
import pytest
from gc_core import processing_responses as pr
from gc_testkit import run_coro


class _FakeAuth:
    def __init__(self):
        self.force_calls = 0

    def get_token(self, force=False):
        if force:
            self.force_calls += 1
        return "tok"


def _ctx(auth):
    return types.SimpleNamespace(
        config_dict={}, auth_connector=auth, logger=None
    )


def _http_401():
    """401 в форме httpx: requests ушёл из зависимостей ядра."""
    request = httpx.Request("POST", "http://backend.invalid/v1")
    resp = httpx.Response(401, request=request)
    return httpx.HTTPStatusError(
        "unauthorized", request=request, response=resp
    )


def test_retry_once_on_401_refreshes_and_succeeds(monkeypatch):
    auth = _FakeAuth()
    calls = {"n": 0}

    async def _once(messages, state, ctx, *, set_previous_response_id=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_401()
        return (["msg"], "rid", {})

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", _once)
    out = run_coro(pr._arequest_backend_canonical([], object(), _ctx(auth)))
    assert out == (["msg"], "rid", {})
    assert calls["n"] == 2 and auth.force_calls == 1


def test_no_retry_when_not_auth_error(monkeypatch):
    auth = _FakeAuth()
    calls = {"n": 0}

    async def _once(*a, **k):
        calls["n"] += 1
        raise ValueError("boom")

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", _once)
    with pytest.raises(ValueError):
        run_coro(pr._arequest_backend_canonical([], object(), _ctx(auth)))
    assert calls["n"] == 1 and auth.force_calls == 0


def test_second_401_after_refresh_propagates(monkeypatch):
    auth = _FakeAuth()
    calls = {"n": 0}

    async def _once(*a, **k):
        calls["n"] += 1
        raise _http_401()

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", _once)
    with pytest.raises(httpx.HTTPStatusError):
        run_coro(pr._arequest_backend_canonical([], object(), _ctx(auth)))
    assert (
        calls["n"] == 2 and auth.force_calls == 1
    )  # ровно 1 refresh, потом наверх


def test_no_auth_connector_no_retry(monkeypatch):
    calls = {"n": 0}

    async def _once(*a, **k):
        calls["n"] += 1
        raise _http_401()

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", _once)
    with pytest.raises(httpx.HTTPStatusError):
        run_coro(pr._arequest_backend_canonical([], object(), _ctx(None)))
    assert calls["n"] == 1
