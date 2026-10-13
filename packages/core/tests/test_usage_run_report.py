"""Фаза 5: дельта прогона и сборка текстового отчёта."""

from gc_core.usage.account import CATEGORY_MAIN, CATEGORY_SUMMARY, UsageAccount
from gc_core.usage.report import build_run_usage_report

_PRICING = {
    "enabled": True,
    "currency": "₽",
    "per_tokens": 1_000_000,
    "models": {"m": {"input": 93, "output": 295}},
}


def test_delta_from_isolates_this_run():
    cumulative = UsageAccount()
    cumulative.add(CATEGORY_MAIN, 100, 20)  # прошлый прогон
    baseline = UsageAccount.from_meta(
        cumulative.to_meta()
    )  # снимок до прогона
    cumulative.add(CATEGORY_MAIN, 50, 10)  # текущий прогон
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
    acc.add(CATEGORY_MAIN, 250_000, 40_000, model="m")
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


def test_build_report_fact_currency_follows_endpoint():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 250_000, 40_000, 0, cost=12.5, billed=True)
    ru = build_run_usage_report(
        acc, _PRICING, "m", "https://routerai.ru/api/v1/chat/completions"
    )
    assert "₽" in ru and "$" not in ru
    usd = build_run_usage_report(
        acc, _PRICING, "m", "https://openrouter.ai/api/v1/chat/completions"
    )
    assert "$" in usd


def test_build_dict_fact_currency_follows_endpoint():
    from gc_core.usage.report import build_run_usage_dict

    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 250_000, 40_000, 0, cost=12.5, billed=True)
    d = build_run_usage_dict(
        acc, _PRICING, "m", "https://routerai.ru/api/v1/chat/completions"
    )
    assert d["categories"][CATEGORY_MAIN]["cost_parts"][0]["currency"] == "₽"
    assert "₽" in d["cost"]["by_currency"]


def test_state_exposes_usage_and_last_run_in_memory():
    # На свежем State: usage пустой, last_run_usage = None (прогон не завершался).
    from gc_core.states import State

    st = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    assert isinstance(st.usage, UsageAccount)
    assert st.usage.is_empty()
    assert st.last_run_usage is None
