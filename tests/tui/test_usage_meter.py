"""render_usage_meter: футер «запрос <tok>/<cost> · сессия <tok>/<cost>».

Правила видимости:
- сессия пуста (нет токенов провайдера) → None (весь блок скрыт);
- тариф выключен → только токены, без цены;
- итога запроса ещё нет → только сегмент сессии.
"""
from usage.account import CATEGORY_FINISH_GATE, CATEGORY_MAIN, UsageAccount
from usage.cost import compute_cost
from usage.report import render_cost_report

from tui.services.usage_meter import format_nonzero_cost, render_usage_meter

_PRICING_ON = {
    "enabled": True,
    "input": 1.0,
    "output": 2.0,
    "currency": "₽",
    "per_tokens": 1_000_000,
}
_PRICING_OFF = {"enabled": False}
_PRICING_RUB = {
    "enabled": True,
    "input": 100,
    "output": 200,
    "currency": "₽",
    "per_tokens": 1_000_000,
}
_PRICING_NO_RATES = {"enabled": True, "currency": "₽", "per_tokens": 1_000_000}
_MODEL = "deepseek/deepseek-v4"


def _meta(input_tokens: int, output_tokens: int) -> dict:
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, input_tokens, output_tokens)
    return acc.to_meta()


def test_hidden_when_session_empty():
    assert render_usage_meter(UsageAccount().to_meta(), None, _PRICING_ON, _MODEL) is None


def test_hidden_when_session_meta_none():
    assert render_usage_meter(None, None, _PRICING_ON, _MODEL) is None


def test_tokens_and_cost_when_pricing_on():
    # Чисто тарифный отчёт (нет факта провайдера) — тильды нет нигде.
    session = _meta(2_000_000, 0)      # total 2.0M, cost 2.0 ₽
    last_run = _meta(1_000_000, 0)     # total 1.0M, cost 1.0 ₽
    m = render_usage_meter(session, last_run, _PRICING_ON, _MODEL)
    assert m is not None
    assert m.plain == "запрос 1.0M/1.00₽  ·  сессия 2.0M/2.00₽"


def test_tokens_only_when_pricing_off():
    session = _meta(2_000_000, 0)
    last_run = _meta(1_000_000, 0)
    m = render_usage_meter(session, last_run, _PRICING_OFF, _MODEL)
    assert m is not None
    assert m.plain == "запрос 1.0M  ·  сессия 2.0M"


def test_session_only_when_no_request_total():
    session = _meta(2_000_000, 0)
    m = render_usage_meter(session, None, _PRICING_ON, _MODEL)
    assert m is not None
    assert m.plain == "сессия 2.0M/2.00₽"


def test_session_only_when_request_meta_empty():
    session = _meta(2_000_000, 0)
    m = render_usage_meter(session, UsageAccount().to_meta(), _PRICING_ON, _MODEL)
    assert m is not None
    assert m.plain == "сессия 2.0M/2.00₽"


def test_format_nonzero_cost_shows_money():
    assert format_nonzero_cost(_meta(2_000_000, 0), _PRICING_ON, _MODEL) == "2.00 ₽"


def test_format_nonzero_cost_hidden_when_zero_or_no_pricing():
    assert format_nonzero_cost(UsageAccount().to_meta(), _PRICING_ON, _MODEL) is None
    assert format_nonzero_cost(_meta(2_000_000, 0), _PRICING_OFF, _MODEL) is None
    # крошечный usage по тарифу → не прячем, показываем нижний порог (без «~» — факта нет)
    assert format_nonzero_cost(_meta(1, 0), _PRICING_ON, _MODEL) == "<0.01 ₽"


def test_footer_shows_provider_currency_and_tilde_on_mixed():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=10.45, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    text = render_usage_meter(acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m")
    assert "10.5$~" in text.plain


def test_footer_marks_incomplete_sum_on_mixed_currencies():
    # Тарифная часть в другой валюте в цифру не вошла — это «+», а не только «~».
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=10.45, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    text = render_usage_meter(acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m")
    assert "10.5$~+" in text.plain


def test_footer_plain_when_all_billed():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=10.45, billed=True)
    text = render_usage_meter(acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m")
    assert "10.5$" in text.plain
    assert "~" not in text.plain


def test_footer_marks_unpriced_with_plus():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.5, billed=True)
    acc.add(CATEGORY_MAIN, 1000, 100)
    text = render_usage_meter(acc.to_meta(), acc.to_meta(), _PRICING_NO_RATES, "m")
    assert "0.50$+" in text.plain


def test_sessions_shows_tiny_cost_instead_of_hiding():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.002, billed=True)
    assert format_nonzero_cost(acc.to_meta(), _PRICING_RUB, "m") == "<0.01 $"


def test_sessions_shows_fact_on_mixed_currencies():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=10.45, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    # Тарифный остаток осел в другой валюте (₽) и в headline-сумму (только $) не вошёл — «+».
    assert format_nonzero_cost(acc.to_meta(), _PRICING_RUB, "m") == "10.5 $~+"


def test_footer_uses_one_currency_for_both_segments():
    # В сессии есть факт, в текущем ходе — только тарифная часть.
    # Оба сегмента обязаны быть в одной валюте, а не «запрос в рублях, сессия в долларах».
    session = UsageAccount()
    session.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=10.45, billed=True)
    session.add(CATEGORY_MAIN, 500_000, 50_000)
    request = UsageAccount()
    request.add(CATEGORY_MAIN, 500_000, 50_000)
    text = render_usage_meter(session.to_meta(), request.to_meta(), _PRICING_RUB, "m").plain
    assert "₽" not in text


def test_same_currency_mix_marks_tilde_only():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=10.45, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    pricing_usd = {"enabled": True, "currency": "$", "per_tokens": 1_000_000, "input": 100, "output": 200}
    text = render_usage_meter(acc.to_meta(), acc.to_meta(), pricing_usd, "m").plain
    assert "~" in text
    assert "+" not in text


def test_footer_currency_follows_token_share():
    # Валюта блока — та, на которую приходится больше токенов, а не та, где есть факт.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 5_000_000, 200_000)
    acc.add(CATEGORY_FINISH_GATE, 3_000, 100, 0, cost=0.004, billed=True)
    text = render_usage_meter(acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m").plain
    assert "540.0₽" in text
    assert "$" not in text


def test_tilde_only_when_both_sides_present():
    # Чисто тарифный отчёт: тильды нет.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000)
    assert "~" not in render_usage_meter(acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m").plain
    text = render_cost_report(compute_cost(acc, _PRICING_RUB, "m"), model_name="m")
    assert "~" not in text


def test_tilde_present_on_real_mix():
    # Реальное смешение (факт + тариф в сопоставимых долях) — правило Б не должно
    # выродиться в «тильды не бывает никогда».
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=10.45, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    text = render_usage_meter(acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m").plain
    assert "~" in text
