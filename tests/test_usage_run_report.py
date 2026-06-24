"""Фаза 5: дельта прогона и сборка текстового отчёта."""

from usage.account import CATEGORY_MAIN, CATEGORY_SUMMARY, UsageAccount
from usage.report import build_run_usage_report

_PRICING = {
    "enabled": True,
    "currency": "₽",
    "per_tokens": 1_000_000,
    "models": {"m": {"input": 93, "output": 295}},
}


def test_delta_from_isolates_this_run():
    cumulative = UsageAccount()
    cumulative.add(CATEGORY_MAIN, 100, 20)  # прошлый прогон
    baseline = UsageAccount.from_meta(cumulative.to_meta())  # снимок до прогона
    cumulative.add(CATEGORY_MAIN, 50, 10)   # текущий прогон
    cumulative.add(CATEGORY_SUMMARY, 30, 5)

    run = cumulative.delta_from(baseline)
    assert run.by_category[CATEGORY_MAIN]["input"] == 50
    assert run.by_category[CATEGORY_MAIN]["output"] == 10
    assert run.by_category[CATEGORY_SUMMARY]["input"] == 30
    # категории без дельты выпадают
    assert run.totals()["input"] == 80


def test_delta_from_empty_baseline():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 10, 2)
    run = acc.delta_from(UsageAccount())
    assert run.totals()["input"] == 10


def test_build_report_with_pricing():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 250_000, 40_000)
    text = build_run_usage_report(acc, _PRICING, "m")
    assert text is not None
    assert "Run usage" in text
    assert "₽" in text


def test_build_report_no_pricing_tokens_only():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    text = build_run_usage_report(acc, {"enabled": False}, "m")
    assert text is not None
    assert "cost" not in text


def test_build_report_empty_returns_none():
    assert build_run_usage_report(UsageAccount(), _PRICING, "m") is None
