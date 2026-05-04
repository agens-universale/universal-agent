import json
from types import SimpleNamespace

import requests

from tools.simple_tools import fetch_url as fetch_url_module
from tools.simple_tools.fetch_url import FetchUrl
from tools.simple_tools.simple_fetch_url import SimpleFetchUrl


class _Resp:
    def __init__(self, text, content_type="text/html", status_code=200, url="https://x", content=None):
        self.text = text
        if content is None:
            content = text.encode("utf-8")
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        return None


class _SessionStub:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, *_args, **_kwargs):
        return self._response


def _url_policy_config(allowed_schemes=None, url_blacklist=None):
    return {
        "command_moderation": {
            "url_policy": {
                "allowed_schemes": allowed_schemes or ["http", "https"],
                "url_blacklist": url_blacklist or [],
            }
        }
    }


def test_fetch_url_validation():
    tool = FetchUrl()
    assert json.loads(tool(url=""))["error"]["field"] == "url"
    assert json.loads(tool(url="ftp://x"))["error"]["field"] == "url"
    assert json.loads(tool(url="https://x", timeout_seconds=-1))["error"]["field"] == "timeout_seconds"
    assert json.loads(tool(url="https://x", max_preview_chars=0))["error"]["field"] == "max_preview_chars"
    assert json.loads(tool(url="https://x", offset=-1))["error"]["field"] == "offset"
    assert json.loads(tool(url="https://x", limit=0))["error"]["field"] == "limit"


def test_fetch_url_policy_blocks_before_request(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("request should not be executed for blocked URL")

    monkeypatch.setattr(requests, "get", fail_if_called)

    data = json.loads(
        FetchUrl(config_dict=_url_policy_config(url_blacklist=["blocked.example"]))(
            url="https://blocked.example/page"
        )
    )

    assert data["ok"] is False
    assert data["result_type"] == "error"
    assert data["error"]["type"] == "url_blocked_by_policy"
    assert data["error"]["matched_rule"] == "blocked.example"


def test_fetch_url_policy_blocks_disallowed_scheme_before_legacy_validation(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("request should not be executed for blocked URL")

    monkeypatch.setattr(requests, "get", fail_if_called)

    data = json.loads(
        FetchUrl(config_dict=_url_policy_config(allowed_schemes=["https"]))(
            url="http://example.com"
        )
    )

    assert data["ok"] is False
    assert data["error"]["type"] == "url_validation_failed"


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
    assert first["next_offset"] == 15
    assert first["cache_hit"] is False
    assert state.shared.fetch_url_cache["https://example.com"]["content"] == "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    second = json.loads(tool(url="https://example.com", offset=20, limit=10))
    assert second["ok"] is True
    assert second["content"] == "UVWXYZ"
    assert second["returned_chars"] == 6
    assert second["has_more"] is False
    assert second["next_offset"] is None
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
    assert data["next_offset"] is None
    assert data["cache_hit"] is True


def test_fetch_url_does_not_cache_binary(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp("", content_type="application/pdf"))

    state = SimpleNamespace(shared=SimpleNamespace(fetch_url_cache={}))
    data = json.loads(FetchUrl(state=state)(url="https://example.com/a.pdf"))
    assert data["ok"] is True
    assert data["is_binary"] is True
    assert state.shared.fetch_url_cache == {}


def test_fetch_url_marks_unknown_binary_bytes_as_binary(monkeypatch):
    binary_payload = b"\x00\x89\x10\x02\x00"
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _Resp(
            "",
            content_type="",
            url="https://example.com/blob",
            content=binary_payload,
        ),
    )
    data = json.loads(FetchUrl()(url="https://example.com/blob"))
    assert data["ok"] is True
    assert data["is_binary"] is True
    assert data["result_type"] == "binary"


def test_simple_fetch_url_extracts_html_and_supports_pagination(monkeypatch):
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(
            _Resp(
                "<html><body><div>One <b>Two</b></div><p>Three</p></body></html>",
                content_type="text/html",
                url="https://example.com",
            )
        ),
    )
    state = SimpleNamespace(shared=SimpleNamespace(fetch_url_cache={}))
    tool = SimpleFetchUrl(state=state)
    first = json.loads(tool(url="https://example.com", offset=0, limit=7))
    second = json.loads(tool(url="https://example.com", offset=7, limit=50))

    assert first["ok"] is True
    assert first["result_type"] == "content"
    assert first["cache_hit"] is False
    assert first["next_offset"] == 7
    assert second["ok"] is True
    assert second["cache_hit"] is True
    assert second["next_offset"] is None
    assert second["content"] != ""


def test_simple_fetch_url_policy_blocks_before_request(monkeypatch):
    def fail_session_if_called():
        raise AssertionError("session should not be created for blocked URL")

    monkeypatch.setattr(requests, "Session", fail_session_if_called)

    data = json.loads(
        SimpleFetchUrl(config_dict=_url_policy_config(url_blacklist=["*.internal"]))(
            url="https://api.internal/path"
        )
    )

    assert data["ok"] is False
    assert data["result_type"] == "error"
    assert data["error"]["type"] == "url_blocked_by_policy"
    assert data["error"]["matched_rule"] == "*.internal"


def test_simple_fetch_url_handles_lists_headings_pre_and_tables(monkeypatch):
    html = (
        "<html><body>"
        "<h2>Title</h2>"
        "<ul><li>first</li><li>second</li></ul>"
        "<pre>line1\nline2</pre>"
        "<table><tr><td>a</td><td>b</td></tr></table>"
        "</body></html>"
    )
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(html, content_type="text/html", url="https://example.com")),
    )
    data = json.loads(SimpleFetchUrl()(url="https://example.com"))
    text = data["content"]
    assert data["ok"] is True
    assert "## Title" in text
    assert "- first" in text and "- second" in text
    assert "line1\nline2" in text
    assert "a" in text and "b" in text


def test_simple_fetch_url_does_not_mark_utf8_html_as_binary(monkeypatch):
    html = "<html><body><p>Привет, мир!</p></body></html>"
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(
            _Resp(
                html,
                content_type="text/html; charset=utf-8",
                url="https://example.com",
                content=html.encode("utf-8"),
            )
        ),
    )
    data = json.loads(SimpleFetchUrl()(url="https://example.com"))
    assert data["ok"] is True
    assert data["is_binary"] is False
    assert data["result_type"] == "content"
