import json
import types
import pytest
import requests
from utils import connect_backend


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200
        self.headers = {}
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_lines(self):
        for ln in self._lines:
            yield ln.encode("utf-8")


def test_streaming_path_used_when_on_delta_given(monkeypatch):
    lines = [
        'data: {"id":"x","choices":[{"index":0,"delta":{"content":"hi"}}]}',
        'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":1}}',
        'data: [DONE]',
    ]
    captured = {}
    def fake_post(url, json=None, headers=None, stream=False, verify=True, **kw):
        captured["stream"] = stream
        return _FakeStreamResponse(lines)
    monkeypatch.setattr(connect_backend._NO_NETRC_SESSION, "post", fake_post)

    deltas = []
    res = connect_backend.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x", verify=True,
        on_delta=lambda k, t: deltas.append((k, t)),
    )
    assert captured["stream"] is True
    assert res["choices"][0]["message"]["content"] == "hi"
    assert res["usage"] == {"completion_tokens": 1}
    assert ("answer", "hi") in deltas


def test_blocking_path_when_no_on_delta(monkeypatch):
    def fake_post(url, json=None, headers=None, stream=False, verify=True, **kw):
        assert stream is False
        r = types.SimpleNamespace()
        r.raise_for_status = lambda: None
        r.json = lambda: {"choices": [{"message": {"content": "blk"}}]}
        return r
    monkeypatch.setattr(connect_backend._NO_NETRC_SESSION, "post", fake_post)
    res = connect_backend.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x", verify=True,
    )
    assert res["choices"][0]["message"]["content"] == "blk"


def test_streaming_emits_reset_before_content(monkeypatch):
    """on_delta("reset", "") must be the very first call before any content delta."""
    lines = [
        'data: {"id":"x","choices":[{"index":0,"delta":{"content":"hi"}}]}',
        'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":1}}',
        'data: [DONE]',
    ]

    def fake_post(url, json=None, headers=None, stream=False, verify=True, **kw):
        return _FakeStreamResponse(lines)

    monkeypatch.setattr(connect_backend._NO_NETRC_SESSION, "post", fake_post)

    deltas = []
    connect_backend.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x", verify=True,
        on_delta=lambda k, t: deltas.append((k, t)),
    )
    assert deltas[0] == ("reset", ""), f"Expected reset first, got: {deltas[0]}"
    assert ("answer", "hi") in deltas


def test_streaming_http_error_reraises(monkeypatch):
    """Streaming branch: HTTP error is re-raised without crashing in the logging/print block."""
    class _ErrorResponse:
        status_code = 500
        text = "Internal Server Error"
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self):
            err = requests.exceptions.HTTPError("500 Server Error")
            err.response = self
            raise err
        def iter_lines(self): return iter([])

    def fake_post(url, json=None, headers=None, stream=False, verify=True, **kw):
        return _ErrorResponse()

    monkeypatch.setattr(connect_backend._NO_NETRC_SESSION, "post", fake_post)

    with pytest.raises(requests.exceptions.HTTPError):
        connect_backend.request_backend_chat_completions(
            {"model": "m", "messages": []}, {}, url="http://x", verify=True,
            use_retry=False,
            on_delta=lambda k, t: None,
        )
