import json
from types import SimpleNamespace

import requests

from tools.simple_tools import fetch_url as fetch_url_module
from tools.simple_tools.fetch_url import FetchUrl


class _Resp:
    def __init__(self, text, content_type="text/html", status_code=200, url="https://x"):
        self.text = text
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        return None


def test_fetch_url_validation():
    tool = FetchUrl()
    assert json.loads(tool(url=""))["error"]["field"] == "url"
    assert json.loads(tool(url="ftp://x"))["error"]["field"] == "url"
    assert json.loads(tool(url="https://x", timeout_seconds=-1))["error"]["field"] == "timeout_seconds"
    assert json.loads(tool(url="https://x", max_preview_chars=0))["error"]["field"] == "max_preview_chars"
    assert json.loads(tool(url="https://x", offset=-1))["error"]["field"] == "offset"
    assert json.loads(tool(url="https://x", limit=0))["error"]["field"] == "limit"


def test_fetch_url_binary(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp("", content_type="application/pdf"))
    data = json.loads(FetchUrl()(url="https://example.com/a.pdf"))
    assert data["ok"] is True and data["is_binary"] is True and data["content"] == "binary data"


def test_fetch_url_extraction_fallback(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp("<html><body><p>hello world</p></body></html>"))
    monkeypatch.setattr(fetch_url_module.trafilatura, "extract", lambda *a, **k: None)
    monkeypatch.setattr(fetch_url_module, "Document", lambda *a, **k: SimpleNamespace(summary=lambda html_partial=True: "<p>fallback</p>"))
    data = json.loads(FetchUrl()(url="https://example.com"))
    assert data["ok"] is True
    assert data["is_binary"] is False
    assert data["content"]


def test_fetch_url_request_failure(monkeypatch):
    def _raise(*_a, **_k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "get", _raise)
    data = json.loads(FetchUrl()(url="https://example.com"))
    assert data["ok"] is False
    assert data["error"]["type"] == "request_failed"


def test_fetch_url_caches_full_content_and_slices(monkeypatch):
    calls = {"n": 0}

    def _get(*_a, **_k):
        calls["n"] += 1
        return _Resp("<html></html>", url="https://example.com")

    monkeypatch.setattr(requests, "get", _get)
    monkeypatch.setattr(fetch_url_module.trafilatura, "extract", lambda *a, **k: "ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    state = SimpleNamespace(shared=SimpleNamespace(fetch_url_cache={}))
    tool = FetchUrl(state=state)

    first = json.loads(tool(url="https://example.com", offset=5, limit=10))
    assert first["ok"] is True
    assert first["content"] == "FGHIJKLMNO"
    assert first["total_chars"] == 26
    assert first["returned_chars"] == 10
    assert first["has_more"] is True
    assert first["cache_hit"] is False
    assert state.shared.fetch_url_cache["https://example.com"] == "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    second = json.loads(tool(url="https://example.com", offset=20, limit=10))
    assert second["ok"] is True
    assert second["content"] == "UVWXYZ"
    assert second["returned_chars"] == 6
    assert second["has_more"] is False
    assert second["cache_hit"] is True
    assert calls["n"] == 1


def test_fetch_url_offset_beyond_end_returns_empty_from_cache(monkeypatch):
    monkeypatch.setattr(fetch_url_module.trafilatura, "extract", lambda *a, **k: "short")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp("<html></html>", url="https://example.com"))

    state = SimpleNamespace(shared=SimpleNamespace(fetch_url_cache={}))
    tool = FetchUrl(state=state)
    _ = json.loads(tool(url="https://example.com", offset=0, limit=5))

    data = json.loads(tool(url="https://example.com", offset=99, limit=5))
    assert data["ok"] is True
    assert data["content"] == ""
    assert data["returned_chars"] == 0
    assert data["has_more"] is False
    assert data["cache_hit"] is True


def test_fetch_url_does_not_cache_binary(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp("", content_type="application/pdf"))

    state = SimpleNamespace(shared=SimpleNamespace(fetch_url_cache={}))
    data = json.loads(FetchUrl(state=state)(url="https://example.com/a.pdf"))
    assert data["ok"] is True
    assert data["is_binary"] is True
    assert state.shared.fetch_url_cache == {}
