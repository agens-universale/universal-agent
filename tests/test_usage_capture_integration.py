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
    monkeypatch.setattr(cb.requests, "post", lambda *a, **k: _FakeResponse(payload))


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
