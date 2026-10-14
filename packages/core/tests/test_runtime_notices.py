"""Runtime notices: LLM failures → ephemeral Notice list."""

from __future__ import annotations

import httpx
import pytest
from gc_core import request_backend_policy as policy
from gc_core.runtime_notices import (
    clear_runtime_notices,
    collect_runtime_notices,
    notice_from_llm_failure,
    push_runtime_notice,
)
from gc_core.startup_notices import Notice
from gc_core.utils.backend_errors import EmbeddedChoiceError
from gc_testkit import as_coro, run_coro


def _http_error(status: int, body: dict) -> httpx.HTTPStatusError:
    """Ошибка ровно того типа, который летит из ядра: оно ходит через httpx."""
    request = httpx.Request(
        "POST", "http://backend.invalid/v1/chat/completions"
    )
    resp = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("http error", request=request, response=resp)


def setup_function():
    clear_runtime_notices(listeners=True)


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch):
    """Пауза между адаптивными повторами — настоящие 3 с на шаг: без подмены два
    теста провала ждали по 15 с, ничего не проверяя в это время."""
    monkeypatch.setattr(policy, "_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC", 0)


def test_notice_from_http_error_includes_status_and_detail():
    exc = _http_error(
        400,
        {"message": "Input should be 'low', 'medium' or 'high'"},
    )
    n = notice_from_llm_failure(
        exc,
        request_label="intent_guard_finish_gate",
        url="http://x/v1/chat/completions",
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
        "provider returned embedded choice error: code=504 message='Upstream idle timeout exceeded'",
        status_code=504,
        raw_code=504,
    )
    n = notice_from_llm_failure(
        exc, request_label="main_agent", url="http://x/v1/chat/completions"
    )
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

    monkeypatch.setattr(
        policy, "arequest_backend_chat_completions", as_coro(boom)
    )

    with pytest.raises(httpx.HTTPStatusError):
        run_coro(
            policy.arequest_chat_completions_with_policy(
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
        )

    notices = collect_runtime_notices()
    assert len(notices) == 1
    assert notices[0].code == "llm_error_400"


def test_policy_skips_notice_when_disabled(monkeypatch):
    clear_runtime_notices()

    def boom(*args, **kwargs):
        raise _http_error(500, {"message": "fail"})

    monkeypatch.setattr(
        policy, "arequest_backend_chat_completions", as_coro(boom)
    )

    with pytest.raises(httpx.HTTPStatusError):
        run_coro(
            policy.arequest_chat_completions_with_policy(
                payload_factory=lambda mt: {"model": "m", "messages": []},
                initial_max_tokens=1400,
                min_response_tokens=1024,
                headers={},
                url="http://test/v1/chat/completions",
                verify=False,
                use_retry=False,
                runtime_notices_enabled=False,
            )
        )

    assert collect_runtime_notices() == []


def test_listener_called_on_push():
    clear_runtime_notices()
    seen = []

    from gc_core.runtime_notices import register_runtime_notices_listener

    register_runtime_notices_listener(lambda: seen.append(1))
    push_runtime_notice(Notice("info", "x", "msg"))
    assert seen == [1]


def test_unregistered_listener_is_dropped():
    """Список подписчиков модульный: без отписки подписчик (у TUI — метод
    приложения) жил до конца процесса и держал приложение целиком."""
    from gc_core import runtime_notices
    from gc_core.runtime_notices import (
        register_runtime_notices_listener,
        unregister_runtime_notices_listener,
    )

    seen = []

    def listener():
        seen.append(1)

    register_runtime_notices_listener(listener)
    unregister_runtime_notices_listener(listener)
    unregister_runtime_notices_listener(listener)  # повтор — не ошибка
    push_runtime_notice(Notice("info", "y", "msg"))

    assert seen == []
    assert listener not in runtime_notices._listeners


def test_level_reroute_notice_names_the_dead_alias_and_the_replacement():
    from gc_core.runtime_notices import notice_from_level_reroute

    notice = notice_from_level_reroute(failed_alias="fast", level="default", model="mid-model")
    assert notice.level == "warning" and notice.code == "level_reroute"
    assert "fast" in notice.message and "default" in notice.message and "mid-model" in notice.message
    assert "активной" not in notice.message
