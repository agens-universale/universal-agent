"""_request_backend_canonical: на 401 (истёкший токен) пере-выпускает токен и
повторяет запрос ровно один раз; прочие ошибки — наверх без ретрая."""
import types

import pytest
import requests

from gc_core import processing_responses as pr


class _FakeAuth:
    def __init__(self):
        self.force_calls = 0

    def get_token(self, force=False):
        if force:
            self.force_calls += 1
        return "tok"


def _ctx(auth):
    return types.SimpleNamespace(config_dict={}, auth_connector=auth, logger=None)


def _http_401():
    resp = requests.Response()
    resp.status_code = 401
    return requests.exceptions.HTTPError(response=resp)


def test_retry_once_on_401_refreshes_and_succeeds(monkeypatch):
    auth = _FakeAuth()
    calls = {"n": 0}

    def _once(messages, state, ctx, *, set_previous_response_id=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_401()
        return (["msg"], "rid", {})

    monkeypatch.setattr(pr, "_request_backend_canonical_once", _once)
    out = pr._request_backend_canonical([], object(), _ctx(auth))
    assert out == (["msg"], "rid", {})
    assert calls["n"] == 2 and auth.force_calls == 1


def test_no_retry_when_not_auth_error(monkeypatch):
    auth = _FakeAuth()
    calls = {"n": 0}

    def _once(*a, **k):
        calls["n"] += 1
        raise ValueError("boom")

    monkeypatch.setattr(pr, "_request_backend_canonical_once", _once)
    with pytest.raises(ValueError):
        pr._request_backend_canonical([], object(), _ctx(auth))
    assert calls["n"] == 1 and auth.force_calls == 0


def test_second_401_after_refresh_propagates(monkeypatch):
    auth = _FakeAuth()
    calls = {"n": 0}

    def _once(*a, **k):
        calls["n"] += 1
        raise _http_401()

    monkeypatch.setattr(pr, "_request_backend_canonical_once", _once)
    with pytest.raises(requests.exceptions.HTTPError):
        pr._request_backend_canonical([], object(), _ctx(auth))
    assert calls["n"] == 2 and auth.force_calls == 1  # ровно 1 refresh, потом наверх


def test_no_auth_connector_no_retry(monkeypatch):
    calls = {"n": 0}

    def _once(*a, **k):
        calls["n"] += 1
        raise _http_401()

    monkeypatch.setattr(pr, "_request_backend_canonical_once", _once)
    with pytest.raises(requests.exceptions.HTTPError):
        pr._request_backend_canonical([], object(), _ctx(None))
    assert calls["n"] == 1
