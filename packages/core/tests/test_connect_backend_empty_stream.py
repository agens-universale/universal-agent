import pytest

from gc_core.utils import connect_backend as cb
from gc_core.utils.chat_completions_stream import BackendStreamError


class _Resp:
    def __init__(self, lines):
        self._lines = lines
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_lines(self): return iter(self._lines)


def test_empty_stream_raises_retriable(monkeypatch):
    # SSE без единого content/finish → пустая сборка → retriable BackendStreamError
    monkeypatch.setattr(cb._NO_NETRC_SESSION, "post", lambda *a, **k: _Resp([b"data: [DONE]"]))
    with pytest.raises(BackendStreamError) as ei:
        cb.request_backend_chat_completions(
            {"model": "m", "messages": []}, {}, url="http://x",
            on_delta=lambda kind, text: None,
        )
    assert ei.value.status_code is None          # None → is_retriable_backend_error True
    assert "empty" in str(ei.value).lower()


def test_nonempty_stream_returns_normally(monkeypatch):
    lines = [
        b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}',
        b'data: {"choices":[{"index":0,"delta":{"content":"!"},"finish_reason":"stop"}]}',
        b"data: [DONE]",
    ]
    monkeypatch.setattr(cb._NO_NETRC_SESSION, "post", lambda *a, **k: _Resp(lines))
    res = cb.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x",
        on_delta=lambda kind, text: None,
    )
    assert res["choices"][0]["message"]["content"] == "hi!"
