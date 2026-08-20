"""Runtime notices: LLM failures → ephemeral Notice list."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

import request_backend_policy as policy
from runtime_notices import (
    clear_runtime_notices,
    collect_runtime_notices,
    notice_from_llm_failure,
    push_runtime_notice,
)
from startup_notices import Notice
from utils.backend_errors import EmbeddedChoiceError


def _http_error(status: int, body: dict) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body).encode("utf-8")
    err = requests.exceptions.HTTPError(response=resp)
    return err


def setup_function():
    clear_runtime_notices(listeners=True)


def test_notice_from_http_error_includes_status_and_detail():
    exc = _http_error(
        400,
        {"message": "Input should be 'low', 'medium' or 'high'"},
    )
    n = notice_from_llm_failure(
        exc, request_label="intent_guard_finish_gate", url="http://x/v1/chat/completions",
    )
    assert n.level == "warning"
    assert n.code == "llm_error_400"
    assert "HTTP 400" in n.message
    assert "судья" in n.message
    assert "reasoning_effort" in n.highlights or "low" in n.message


def test_notice_from_embedded_choice_error_shows_http_status():
    """EmbeddedChoiceError несёт status_code (напр. 504) — в TUI должен попасть
    «HTTP 504», а не обезличенное «Ошибка LLM»."""
    exc = EmbeddedChoiceError(
        "provider returned embedded choice error: code=504 "
        "message='Upstream idle timeout exceeded'",
        status_code=504,
        raw_code=504,
    )
    n = notice_from_llm_failure(exc, request_label="main_agent", url="http://x/v1/chat/completions")
    assert n.code == "llm_error_504"
    assert "HTTP 504" in n.message


def test_push_dedup_same_code_and_message():
    n = Notice("warning", "llm_error_400", "same")
    push_runtime_notice(n)
    push_runtime_notice(n)
    assert len(collect_runtime_notices()) == 1


def test_policy_pushes_notice_on_final_failure(monkeypatch):
    clear_runtime_notices()

    def boom(*args, **kwargs):
        raise _http_error(400, {"message": "bad request"})

    monkeypatch.setattr(policy, "request_backend_chat_completions", boom)

    with pytest.raises(requests.exceptions.HTTPError):
        policy.request_chat_completions_with_policy(
            payload_factory=lambda mt: {"model": "m", "messages": []},
            initial_max_tokens=1400,
            min_response_tokens=1024,
            headers={},
            url="http://test/v1/chat/completions",
            verify=False,
            use_retry=False,
            request_label="intent_guard_finish_gate",
            runtime_notices_enabled=True,
            notice_url="http://test/v1/chat/completions",
        )

    notices = collect_runtime_notices()
    assert len(notices) == 1
    assert notices[0].code == "llm_error_400"


def test_policy_skips_notice_when_disabled(monkeypatch):
    clear_runtime_notices()

    def boom(*args, **kwargs):
        raise _http_error(500, {"message": "fail"})

    monkeypatch.setattr(policy, "request_backend_chat_completions", boom)

    with pytest.raises(requests.exceptions.HTTPError):
        policy.request_chat_completions_with_policy(
            payload_factory=lambda mt: {"model": "m", "messages": []},
            initial_max_tokens=1400,
            min_response_tokens=1024,
            headers={},
            url="http://test/v1/chat/completions",
            verify=False,
            use_retry=False,
            runtime_notices_enabled=False,
        )

    assert collect_runtime_notices() == []


def test_listener_called_on_push():
    clear_runtime_notices()
    seen = []

    from runtime_notices import register_runtime_notices_listener

    register_runtime_notices_listener(lambda: seen.append(1))
    push_runtime_notice(Notice("info", "x", "msg"))
    assert seen == [1]
