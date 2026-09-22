"""Embedded choice error (OpenRouter кладёт ошибку внутрь HTTP-200,
choices[0].error) должна ретраиться адаптивным циклом политики, а не улетать
фатальным ValueError позже в конвертации. Регрессия: прогон H2, триал
ZtjA5Bf — 504 "Upstream idle timeout exceeded" убил агента на 15-м шаге."""
import pytest
from gc_core import request_backend_policy as policy
from gc_core.utils.backend_errors import EmbeddedChoiceError
from gc_testkit import run_coro


def _embedded_504_response():
    return {
        "choices": [
            {
                "error": {
                    "code": 504,
                    "message": "Upstream idle timeout exceeded",
                    "metadata": {"error_type": "timeout"},
                }
            }
        ]
    }


def _ok_response():
    return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


def _patch_transport(monkeypatch, responder):
    """Подменить транспорт политики.

    С Задачи 11 политика зовёт асинхронный транспорт (сама она тоже
    асинхронна, синхронный вход — шим через портал), поэтому и фейк
    корутинный, и пауза между попытками — asyncio.sleep.
    """
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(policy.asyncio, "sleep", _no_sleep)  # без задержек
    calls = {"n": 0}

    async def _fake_request(payload, headers, url, verify, use_retry, logger,
                            on_delta, cert=None, timeout=600, config_proxy=None):
        calls["n"] += 1
        return responder(calls["n"])

    monkeypatch.setattr(policy, "arequest_backend_chat_completions", _fake_request)
    return calls


def _run_policy(**overrides):
    kwargs = dict(
        payload_factory=lambda mt: {"max_tokens": mt},
        initial_max_tokens=8192,
        min_response_tokens=1024,
        headers={},
        url="http://x",
        verify=True,
        use_retry=False,
    )
    kwargs.update(overrides)
    return run_coro(policy.arequest_chat_completions_with_policy(**kwargs))


def test_embedded_504_retried_then_succeeds(monkeypatch):
    calls = _patch_transport(
        monkeypatch, lambda n: _embedded_504_response() if n == 1 else _ok_response()
    )
    res, _payload = _run_policy()
    assert res["choices"][0]["message"]["content"] == "ok"
    assert calls["n"] == 2  # 504-в-200 → адаптивный ретрай → успех


def test_embedded_401_not_retried(monkeypatch):
    calls = _patch_transport(
        monkeypatch,
        lambda _n: {"choices": [{"error": {"code": 401, "message": "bad key"}}]},
    )
    with pytest.raises(EmbeddedChoiceError):
        _run_policy()
    assert calls["n"] == 1  # 401 в skip-наборе — фатал без ретраев


def test_embedded_error_exhaustion_raises(monkeypatch):
    calls = _patch_transport(monkeypatch, lambda _n: _embedded_504_response())
    with pytest.raises(EmbeddedChoiceError):
        _run_policy()
    assert calls["n"] > 1  # адаптивные ретраи были, провайдер не ожил — фаталим


def test_flag_disabled_passes_response_through(monkeypatch):
    calls = _patch_transport(monkeypatch, lambda _n: _embedded_504_response())
    res, _payload = _run_policy(retry_embedded_choice_errors=False)
    assert calls["n"] == 1  # старое поведение: ответ уходит наверх как есть
    assert res["choices"][0]["error"]["code"] == 504


def _finish_error_empty_response():
    # Вторая форма embedded-ошибки (регрессия: прогон H4, триал oqskzA3):
    # finish_reason="error" без error-поля и без контента. Раньше улетала
    # фатальным ValueError в get_output_list уже за retry-границей.
    return {"choices": [{"finish_reason": "error", "message": {}}]}


def test_finish_reason_error_empty_retried_then_succeeds(monkeypatch):
    calls = _patch_transport(
        monkeypatch,
        lambda n: _finish_error_empty_response() if n == 1 else _ok_response(),
    )
    res, _payload = _run_policy()
    assert res["choices"][0]["message"]["content"] == "ok"
    assert calls["n"] == 2  # error-финиш в 200 → адаптивный ретрай → успех


def test_finish_reason_error_with_content_accepted(monkeypatch):
    # Ответ с контентом — пригоден: permissive-путь normalize_finish_reason
    # принимает его (с warning). Ретрай не нужен, исключения нет.
    calls = _patch_transport(
        monkeypatch,
        lambda _n: {
            "choices": [
                {"finish_reason": "error", "message": {"content": "partial"}}
            ]
        },
    )
    res, _payload = _run_policy()
    assert calls["n"] == 1
    assert res["choices"][0]["message"]["content"] == "partial"


def test_finish_reason_error_with_whitespace_content_accepted(monkeypatch):
    # «\n» — payload: не embedded-empty, не ретрай.
    calls = _patch_transport(
        monkeypatch,
        lambda _n: {
            "choices": [
                {"finish_reason": "error", "message": {"content": "\n"}}
            ]
        },
    )
    res, _payload = _run_policy()
    assert calls["n"] == 1
    assert res["choices"][0]["message"]["content"] == "\n"


def _finish_error_reasoning_only():
    return {
        "choices": [
            {
                "finish_reason": "error",
                "message": {"reasoning": "search the leaderboard"},
            }
        ]
    }


def test_finish_reason_error_with_reasoning_is_embedded_without_salvage(monkeypatch):
    # Служебная воронка: reasoning не content. Аборт провайдера — ошибка, не пустая сводка.
    calls = _patch_transport(monkeypatch, lambda _n: _finish_error_reasoning_only())
    with pytest.raises(EmbeddedChoiceError, match="finish_reason='error'"):
        _run_policy()
    assert calls["n"] > 1


def test_finish_reason_error_with_reasoning_accepted_when_salvage(monkeypatch):
    # Агентский цикл: reasoning-only salvage в <think>, не EmbeddedChoiceError.
    calls = _patch_transport(monkeypatch, lambda _n: _finish_error_reasoning_only())
    res, _payload = _run_policy(salvage_reasoning=True)
    assert calls["n"] == 1
    assert res["choices"][0]["message"]["reasoning"] == "search the leaderboard"


def test_embedded_nonnumeric_code_not_retried(monkeypatch):
    """Нечисловой error.code (insufficient_quota/invalid_api_key) → status=None,
    повтором не чинится: один вызов, без пере-отправок промпта. В отличие от
    finish_reason='error' (raw_code=None) — тот ретраится (см. тест ниже)."""
    calls = _patch_transport(
        monkeypatch,
        lambda _n: {
            "choices": [
                {"error": {"code": "insufficient_quota", "message": "no funds"}}
            ]
        },
    )
    with pytest.raises(EmbeddedChoiceError):
        _run_policy()
    assert calls["n"] == 1


def test_embedded_top_level_error_form(monkeypatch):
    """Вторая типовая форма: top-level {"error": {...}} без choices — тоже
    поднимается как EmbeddedChoiceError, а не падает KeyError в конвертации."""
    calls = _patch_transport(
        monkeypatch,
        lambda n: (
            {"error": {"code": 504, "message": "Upstream idle timeout exceeded"}}
            if n == 1
            else _ok_response()
        ),
    )
    res, _payload = _run_policy()
    assert res["choices"][0]["message"]["content"] == "ok"
    assert calls["n"] == 2


def test_embedded_504_retried_when_max_equals_min(monkeypatch):
    """max_tokens == min_response_tokens (напр. 1024/1024): понижать некуда, но
    504 всё равно ретраится — счётчик попыток независим от бюджета токенов."""
    calls = _patch_transport(
        monkeypatch, lambda n: _embedded_504_response() if n == 1 else _ok_response()
    )
    res, _payload = _run_policy(initial_max_tokens=1024, min_response_tokens=1024)
    assert res["choices"][0]["message"]["content"] == "ok"
    assert calls["n"] == 2
