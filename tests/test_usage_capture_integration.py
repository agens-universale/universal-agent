"""Интеграция захвата: connect_backend пишет usage в активный usage_scope."""

import json

import pytest

import utils.connect_backend as cb
from usage.account import CATEGORY_MAIN, CATEGORY_SUMMARY, UsageAccount
from usage.context import usage_scope


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _patch_post(monkeypatch, payload: dict):
    # Транспорт non-stream идёт через _NO_NETRC_SESSION.post (не cb.requests.post).
    monkeypatch.setattr(cb._NO_NETRC_SESSION, "post", lambda *a, **k: _FakeResponse(payload))


def test_chat_completions_capture_with_usage(monkeypatch):
    _patch_post(
        monkeypatch,
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        },
    )
    acc = UsageAccount()
    with usage_scope(acc, CATEGORY_MAIN, "chat_completions"):
        cb.request_backend_chat_completions({}, {}, url="http://x", use_retry=False)
    assert acc.by_category[CATEGORY_MAIN]["input"] == 120
    assert acc.by_category[CATEGORY_MAIN]["output"] == 30
    assert acc.by_category[CATEGORY_MAIN]["calls"] == 1


def test_chat_completions_capture_without_usage(monkeypatch):
    _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    acc = UsageAccount()
    with usage_scope(acc, CATEGORY_SUMMARY, "chat_completions"):
        cb.request_backend_chat_completions({}, {}, url="http://x", use_retry=False)
    assert acc.by_category[CATEGORY_SUMMARY]["missing_usage_calls"] == 1
    assert acc.by_category[CATEGORY_SUMMARY]["input"] == 0


def test_chat_completions_uncategorized_when_no_scope(monkeypatch, caplog):
    _patch_post(
        monkeypatch,
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        },
    )
    # Вне usage_scope — не падаем, фиксируем предупреждение (детектор дыр).
    with caplog.at_level("WARNING"):
        cb.request_backend_chat_completions({}, {}, url="http://x", use_retry=False)
    assert any("usage_scope" in r.message for r in caplog.records)


# ---------------- streaming path (основной цикл агента) ----------------

class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for ln in self._lines:
            yield ln.encode("utf-8")


_STREAM_LINES = [
    'data: {"id":"x","choices":[{"index":0,"delta":{"content":"hi"}}]}',
    'data: {"id":"x","choices":[],"usage":{"prompt_tokens":200,"completion_tokens":50}}',
    'data: [DONE]',
]


def test_streaming_capture_with_usage(monkeypatch):
    """Основной цикл идёт стримом (on_delta задан) — usage из финального чанка
    должен доезжать в активный usage_scope (регресс: раньше терялся)."""
    monkeypatch.setattr(
        cb._NO_NETRC_SESSION, "post",
        lambda *a, **k: _FakeStreamResponse(_STREAM_LINES),
    )
    acc = UsageAccount()
    with usage_scope(acc, CATEGORY_MAIN, "chat_completions"):
        cb.request_backend_chat_completions(
            {"model": "m", "messages": []}, {}, url="http://x",
            on_delta=lambda k, t: None,
        )
    assert acc.by_category[CATEGORY_MAIN]["input"] == 200
    assert acc.by_category[CATEGORY_MAIN]["output"] == 50
    assert acc.by_category[CATEGORY_MAIN]["calls"] == 1


def test_streaming_requests_usage_via_stream_options(monkeypatch):
    """Провайдер отдаёт usage в стриме только при stream_options.include_usage=True —
    payload должен его запрашивать, иначе финального usage-чанка не будет."""
    captured = {}

    def fake_post(url, json=None, headers=None, stream=False, verify=True, **kw):
        captured["payload"] = json
        return _FakeStreamResponse(_STREAM_LINES)

    monkeypatch.setattr(cb._NO_NETRC_SESSION, "post", fake_post)
    cb.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x",
        on_delta=lambda k, t: None,
    )
    assert captured["payload"].get("stream") is True
    assert captured["payload"].get("stream_options") == {"include_usage": True}
