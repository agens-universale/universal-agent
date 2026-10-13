"""Tariff fallback follows actual models, including billed subsets and resume."""

import copy

import pytest
from gc_core.usage.account import UsageAccount
from gc_core.usage.cost import compute_cost

PRICING = {
    "enabled": True, "currency": "USD", "per_tokens": 1000,
    "models": {"fast": {"input": 1, "output": 2, "cache_read": 0.1},
               "big": {"input": 10, "output": 20, "cache_read": 1}},
}


def test_cost_uses_each_actual_model_tariff():
    account = UsageAccount()
    account.add("main", 1000, 0, model="fast")
    account.add("main", 1000, 0, model="big")
    assert compute_cost(account, PRICING, "fast").by_currency == {"USD": 11}


def test_cost_does_not_price_provider_billed_tokens_again():
    account = UsageAccount()
    account.add("main", 1000, 0, model="fast", billed=True, cost=7, currency="USD")
    account.add("main", 1000, 0, model="big")
    report = compute_cost(account, PRICING, "fast")
    assert report.by_currency == {"USD": 17}
    assert report.categories[0].cost_source == "mixed"
    assert not report.has_unpriced


def test_cost_uses_model_cache_and_output_rates():
    account = UsageAccount()
    account.add("summary", 1000, 100, cached_tokens=500, model="fast")
    account.add("summary", 1000, 100, cached_tokens=500, model="big")
    assert compute_cost(account, PRICING, "fast").by_currency["USD"] == pytest.approx(8.25)


def test_unknown_model_preserves_known_subtotal_and_marks_incomplete():
    account = UsageAccount()
    account.add("main", 1000, 0, model="fast")
    account.add("main", 1000, 0, model="unknown")
    report = compute_cost(account, PRICING, "fast")
    assert report.by_currency == {"USD": 1}
    assert report.has_unpriced


def test_legacy_category_without_model_does_not_invent_a_tariff():
    account = UsageAccount.from_meta({
        "by_category": {"main": {"input": 2000, "calls": 2}},
        "by_model": {
            "fast": {"input": 1000, "calls": 1},
            "big": {"input": 1000, "calls": 1},
        },
    })
    report = compute_cost(account, PRICING, "fast")
    assert report.by_currency == {}
    assert report.has_unpriced


def test_legacy_billed_money_is_preserved_with_unknown_tariff_remainder():
    account = UsageAccount()
    account.add("main", 1000, 0, billed=True, cost=7, currency="USD")
    account.add("main", 1000, 0)
    account = UsageAccount.from_meta(account.to_meta())
    report = compute_cost(account, PRICING, "fast")
    assert report.by_currency == {"USD": 7}
    assert report.has_unpriced


def test_new_calls_after_legacy_resume_price_only_known_models():
    account = UsageAccount.from_meta({"by_category": {"main": {"input": 1000, "calls": 1}}})
    baseline = UsageAccount.from_meta(account.to_meta())
    account.add("main", 1000, 0, model="big")
    assert compute_cost(account, PRICING, "fast").by_currency == {"USD": 10}
    assert compute_cost(account, PRICING, "fast").has_unpriced
    delta = account.delta_from(baseline)
    assert compute_cost(delta, PRICING, "fast").by_currency == {"USD": 10}
    assert not compute_cost(delta, PRICING, "fast").has_unpriced


def test_merge_and_delta_preserve_category_model_billing():
    account = UsageAccount()
    account.add("main", 1000, 0, model="fast")
    baseline = UsageAccount.from_meta(account.to_meta())
    child = UsageAccount()
    child.add("main", 1000, 0, model="big")
    child.add("summary", 1000, 0, model="fast", billed=True, cost=7, currency="USD")
    account.merge(child, as_category="subagent")
    restored = UsageAccount.from_meta(account.to_meta())
    assert compute_cost(restored, PRICING, "fast").by_currency == {"USD": 18}
    delta = restored.delta_from(baseline)
    assert compute_cost(delta, PRICING, "fast").by_currency == {"USD": 17}
    assert {c.category for c in compute_cost(delta, PRICING, "fast").categories} == {"subagent"}
    assert restored.totals()["input"] == 3000
    assert restored.by_model["fast"]["input"] == 2000


def test_meta_model_attribution_is_a_deep_snapshot():
    account = UsageAccount()
    account.add("main", 1000, 0, model="big")
    snapshot = account.to_meta()
    unchanged = copy.deepcopy(snapshot)
    account.add("main", 1000, 0, model="big", billed=True, cost=7, currency="USD")
    assert snapshot == unchanged
    restored = UsageAccount.from_meta(snapshot)
    assert compute_cost(restored, PRICING, "fast").by_currency == {"USD": 10}


def test_cost_keeps_currencies_separate():
    pricing = copy.deepcopy(PRICING)
    pricing["models"]["big"]["currency"] = "EUR"
    account = UsageAccount()
    account.add("main", 1000, 0, model="fast")
    account.add("main", 1000, 0, model="big")
    report = compute_cost(account, pricing, "fast")
    assert report.by_currency == {"USD": 1, "EUR": 10}
    assert report.multi_currency


def test_global_tariff_remains_the_explicit_default_for_actual_models():
    account = UsageAccount()
    account.add("main", 1000, 0, model="big")
    pricing = {"enabled": True, "currency": "USD", "per_tokens": 1000, "input": 3, "output": 0}
    assert compute_cost(account, pricing, "unrelated").by_currency == {"USD": 3}


def test_disabled_pricing_does_not_show_money():
    account = UsageAccount()
    account.add("main", 1000, 0, model="big")
    report = compute_cost(account, {**PRICING, "enabled": False}, "fast")
    assert not report.has_cost
    assert not report.has_unpriced
    assert report.total_input == 1000



def test_record_usage_attributes_actual_request_models():
    from gc_core.usage.context import record_usage, usage_scope

    account = UsageAccount()
    with usage_scope(account, "main", "chat_completions"):
        record_usage({"prompt_tokens": 1000, "completion_tokens": 0}, model="fast")
        record_usage({"prompt_tokens": 1000, "completion_tokens": 0}, model="big")
    assert compute_cost(account, PRICING, "fast").by_currency == {"USD": 11}


def test_missing_usage_survives_model_snapshot_and_delta():
    account = UsageAccount()
    account.add_missing("main", model="fast")
    baseline = UsageAccount.from_meta(account.to_meta())
    account.add_missing("main", model="big")
    delta = UsageAccount.from_meta(account.to_meta()).delta_from(baseline)
    assert delta.by_category["main"]["by_model"]["big"]["missing_usage_calls"] == 1
    assert delta.by_category["main"]["missing_usage_calls"] == 1
    assert delta.by_model["big"]["missing_usage_calls"] == 1


def test_tariff_and_unpriced_can_coexist_in_one_category():
    from gc_core.usage.report import render_cost_report

    account = UsageAccount()
    account.add("main", 1000, 0, model="fast")
    account.add("main", 1000, 0)
    report = compute_cost(account, PRICING, "fast")
    assert report.categories[0].cost_source == "tariff"
    assert report.categories[0].unpriced
    text = render_cost_report(report)
    assert "1.00 USD?" in text
    assert "итог неполный" in text
