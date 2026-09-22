"""Интеграция захвата: connect_backend пишет usage в активный usage_scope."""

import gc_core.utils.connect_backend as cb
from gc_core.usage.account import CATEGORY_MAIN, CATEGORY_SUMMARY, MONEY_FIELD, UsageAccount
from gc_core.usage.context import usage_scope
from gc_testkit import run_coro
from transport_fakes import install_transport, json_response, sse_body, stream_response


def _patch_post(monkeypatch, payload: dict):
    # Транспорт запросов к модели — httpx-клиент (Задача 9 async-переезда).
    return install_transport(monkeypatch, json_response(payload))


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
        run_coro(cb.arequest_backend_chat_completions({}, {}, url="http://x", use_retry=False))
    assert acc.by_category[CATEGORY_MAIN]["input"] == 120
    assert acc.by_category[CATEGORY_MAIN]["output"] == 30
    assert acc.by_category[CATEGORY_MAIN]["calls"] == 1


def test_chat_completions_capture_without_usage(monkeypatch):
    _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    acc = UsageAccount()
    with usage_scope(acc, CATEGORY_SUMMARY, "chat_completions"):
        run_coro(cb.arequest_backend_chat_completions({}, {}, url="http://x", use_retry=False))
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
        run_coro(cb.arequest_backend_chat_completions({}, {}, url="http://x", use_retry=False))
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


_STREAM_BODY = sse_body(
    {"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
    {"id": "x", "choices": [], "usage": {"prompt_tokens": 200, "completion_tokens": 50}},
)


def test_streaming_capture_with_usage(monkeypatch):
    """Основной цикл идёт стримом (on_delta задан) — usage из финального чанка
    должен доезжать в активный usage_scope (регресс: раньше терялся)."""
    install_transport(monkeypatch, stream_response(_STREAM_BODY))
    acc = UsageAccount()
    with usage_scope(acc, CATEGORY_MAIN, "chat_completions"):
        run_coro(
            cb.arequest_backend_chat_completions(
                {"model": "m", "messages": []},
                {},
                url="http://x",
                on_delta=lambda k, t: None,
            )
        )
    assert acc.by_category[CATEGORY_MAIN]["input"] == 200
    assert acc.by_category[CATEGORY_MAIN]["output"] == 50
    assert acc.by_category[CATEGORY_MAIN]["calls"] == 1


def test_streaming_requests_usage_via_stream_options(monkeypatch):
    """Провайдер отдаёт usage в стриме только при stream_options.include_usage=True —
    payload должен его запрашивать, иначе финального usage-чанка не будет."""
    spy = install_transport(monkeypatch, stream_response(_STREAM_BODY))

    run_coro(
        cb.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            on_delta=lambda k, t: None,
        )
    )
    assert spy.last_payload.get("stream") is True
    assert spy.last_payload.get("stream_options") == {"include_usage": True}


# -------- Task 3: фактическая стоимость от провайдера --------


def test_record_usage_delivers_cost():
    """Фактическая стоимость от провайдера доезжает в накопитель через record_usage."""
    from gc_core.usage.context import record_usage

    account = UsageAccount()
    with usage_scope(account, CATEGORY_MAIN):
        record_usage(
            {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.0058},
            "https://openrouter.ai/api/v1/chat/completions",
        )
    assert account.by_category[CATEGORY_MAIN][MONEY_FIELD] == {"$": 0.0058}
    assert account.by_category[CATEGORY_MAIN]["billed_calls"] == 1


def test_record_usage_fixes_currency_by_endpoint():
    """Валюта берётся из адреса ИМЕННО ЭТОГО вызова, а не из общего конфига:
    смена провайдера посреди треда не должна складывать рубли с долларами."""
    from gc_core.usage.context import record_usage

    account = UsageAccount()
    with usage_scope(account, CATEGORY_MAIN):
        record_usage(
            {"prompt_tokens": 100, "completion_tokens": 10, "cost": 2.0},
            "https://openrouter.ai/api/v1/chat/completions",
        )
        record_usage(
            {"prompt_tokens": 100, "completion_tokens": 10, "cost": 200.0}, "https://routerai.ru/v1/chat/completions"
        )
    assert account.by_category[CATEGORY_MAIN][MONEY_FIELD] == {"$": 2.0, "₽": 200.0}


def test_record_usage_without_url_keeps_currency_unknown():
    """Без адреса валюта не выдумывается: сумма ждёт разрешения при показе."""
    from gc_core.usage.account import LEGACY_CURRENCY
    from gc_core.usage.context import record_usage

    account = UsageAccount()
    with usage_scope(account, CATEGORY_MAIN):
        record_usage({"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.5})
    assert account.by_category[CATEGORY_MAIN][MONEY_FIELD] == {LEGACY_CURRENCY: 0.5}


def test_orphan_account_delivers_cost():
    """Фактическая стоимость записывается в orphan-аккаунт при отсутствии контекста."""
    from gc_core.usage.context import _orphan_account

    before = _orphan_account.account.totals()[MONEY_FIELD].get("$", 0.0)
    from gc_core.usage.context import record_usage

    record_usage(
        {"prompt_tokens": 10, "completion_tokens": 1, "cost": 0.25}, "https://openrouter.ai/api/v1/chat/completions"
    )
    assert _orphan_account.account.totals()[MONEY_FIELD]["$"] == before + 0.25
