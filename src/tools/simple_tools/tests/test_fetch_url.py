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
