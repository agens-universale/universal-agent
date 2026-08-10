from usage.account import (
    CATEGORY_FINISH_GATE,
    CATEGORY_MAIN,
    CATEGORY_SUMMARY,
    CATEGORY_SUBAGENT,
    UsageAccount,
    category_for_request_label,
)
from usage.context import current_usage_context, record_usage, usage_scope
from usage.cost import compute_cost
from usage.normalize import normalize_usage
from usage.report import format_money, format_tokens, render_cost_report


# ---------------- normalize ----------------

def test_normalize_chat_completions_shape():
    assert normalize_usage({"prompt_tokens": 100, "completion_tokens": 20}) == (100, 20, 0)


def test_normalize_responses_shape():
    assert normalize_usage({"input_tokens": 5, "output_tokens": 7}) == (5, 7, 0)


def test_normalize_missing_returns_none():
    assert normalize_usage(None) is None
    assert normalize_usage({}) is None
    assert normalize_usage({"foo": "bar"}) is None
    assert normalize_usage("garbage") is None
    assert normalize_usage({"prompt_tokens": "x"}) is None


def test_normalize_partial_fills_zero():
    assert normalize_usage({"prompt_tokens": 10}) == (10, 0, 0)
    assert normalize_usage({"completion_tokens": 3}) == (0, 3, 0)


def test_normalize_cached_openai_style():
    # RouterAI/OpenAI: prompt_tokens_details.cached_tokens
    u = {"prompt_tokens": 14722, "completion_tokens": 50,
         "prompt_tokens_details": {"cached_tokens": 14464}}
    assert normalize_usage(u) == (14722, 50, 14464)


def test_normalize_cached_deepseek_native():
    # DeepSeek-native: prompt_cache_hit_tokens
    u = {"prompt_tokens": 1000, "completion_tokens": 10, "prompt_cache_hit_tokens": 800}
    assert normalize_usage(u) == (1000, 10, 800)


def test_normalize_cached_clamped_to_input():
    u = {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 999}}
    assert normalize_usage(u) == (100, 5, 100)  # cached не превышает input


def test_normalize_no_cache_details_zero():
    u = {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": None}
    assert normalize_usage(u) == (100, 5, 0)


# ---------------- account ----------------

def test_account_add_and_totals():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    acc.add(CATEGORY_MAIN, 50, 10)
    acc.add(CATEGORY_SUMMARY, 30, 5)
    t = acc.totals()
    assert t["input"] == 180 and t["output"] == 35 and t["total"] == 215
    assert t["calls"] == 3


def test_account_add_missing():
    acc = UsageAccount()
    acc.add_missing(CATEGORY_MAIN)
    t = acc.totals()
    assert t["input"] == 0 and t["output"] == 0
    assert t["calls"] == 1 and t["missing_usage_calls"] == 1


def test_account_merge_as_category():
    parent = UsageAccount()
    parent.add(CATEGORY_MAIN, 100, 20)
    child = UsageAccount()
    child.add(CATEGORY_MAIN, 40, 8)
    child.add(CATEGORY_SUMMARY, 10, 2)
    parent.merge(child, as_category=CATEGORY_SUBAGENT)
    assert parent.by_category[CATEGORY_SUBAGENT]["input"] == 50
    assert parent.by_category[CATEGORY_SUBAGENT]["output"] == 10
    assert parent.by_category[CATEGORY_MAIN]["input"] == 100


def test_account_meta_roundtrip():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    acc.add_missing(CATEGORY_FINISH_GATE)
    restored = UsageAccount.from_meta(acc.to_meta())
    assert restored.totals() == acc.totals()
    assert UsageAccount.from_meta(None).is_empty()
    assert UsageAccount.from_meta({"garbage": 1}).is_empty()


def test_label_to_category():
    assert category_for_request_label("summary_batch_1") == CATEGORY_SUMMARY
    assert category_for_request_label("intent_guard_finish_gate") == CATEGORY_FINISH_GATE
    assert category_for_request_label("command_moderation_judge") == "command_moderation"
    assert category_for_request_label("project_memory_compaction") == "project_memory"
    assert category_for_request_label("intent_scope_summary") == "intent_scope"
    assert category_for_request_label("") == CATEGORY_MAIN
    assert category_for_request_label("whatever") == CATEGORY_MAIN


def test_label_to_category_subagent_prefix():
    """Регресс (ревью Task 7): вспомогательные LLM-вызовы субагентов (напр.
    special_agent role generation, request_label="subagent_special_agent") должны
    падать в категорию subagent, не main — request_chat_completion_text резолвит
    категорию именно через этот маппер (usage/account.py)."""
    assert category_for_request_label("subagent_special_agent") == CATEGORY_SUBAGENT


# ---------------- context ----------------

def test_usage_scope_records_to_account():
    acc = UsageAccount()
    with usage_scope(acc, CATEGORY_MAIN, "chat_completions"):
        assert current_usage_context() is not None
        record_usage({"prompt_tokens": 100, "completion_tokens": 20})
    assert current_usage_context() is None
    assert acc.by_category[CATEGORY_MAIN]["input"] == 100


def test_usage_scope_missing_usage():
    acc = UsageAccount()
    with usage_scope(acc, CATEGORY_SUMMARY):
        record_usage(None)
    assert acc.by_category[CATEGORY_SUMMARY]["missing_usage_calls"] == 1


def test_usage_scope_none_is_noop():
    with usage_scope(None, CATEGORY_MAIN):
        record_usage({"prompt_tokens": 1, "completion_tokens": 1})  # не падает
    assert current_usage_context() is None


def test_usage_scope_nesting():
    outer, inner = UsageAccount(), UsageAccount()
    with usage_scope(outer, CATEGORY_MAIN):
        with usage_scope(inner, CATEGORY_SUMMARY):
            record_usage({"prompt_tokens": 5, "completion_tokens": 1})
        record_usage({"prompt_tokens": 10, "completion_tokens": 2})
    assert inner.by_category[CATEGORY_SUMMARY]["input"] == 5
    assert outer.by_category[CATEGORY_MAIN]["input"] == 10


# ---------------- cost ----------------

_PRICING = {
    "enabled": True,
    "currency": "₽",
    "per_tokens": 1_000_000,
    "models": {"m": {"input": 93, "output": 295}},
}


def test_compute_cost_basic():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 250_000, 40_000)
    report = compute_cost(acc, _PRICING, "m")
    assert report.has_pricing is True
    # 250000/1e6*93 + 40000/1e6*295 = 23.25 + 11.8 = 35.05
    assert abs(report.by_currency["₽"] - 35.05) < 1e-6
    assert not report.multi_currency


def test_compute_cost_no_tariff():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    report = compute_cost(acc, _PRICING, "unknown")
    assert report.has_pricing is False
    assert report.categories[0].cost is None


def test_compute_cost_disabled():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    report = compute_cost(acc, {**_PRICING, "enabled": False}, "m")
    assert report.has_pricing is False


_PRICING_CACHE = {
    "enabled": True,
    "currency": "₽",
    "per_tokens": 1_000_000,
    "models": {"deepseek": {"input": 59, "output": 118, "cache_read": 0.49}},
}


def test_compute_cost_with_cache_read():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 800)  # 800 из 1000 input — cached
    report = compute_cost(acc, _PRICING_CACHE, "deepseek")
    # (1000-800)/1e6*59 + 800/1e6*0.49 + 100/1e6*118
    expected = 200 / 1e6 * 59 + 800 / 1e6 * 0.49 + 100 / 1e6 * 118
    assert abs(report.by_currency["₽"] - expected) < 1e-9
    assert report.total_cached == 800


def test_compute_cost_cache_absent_bills_as_input():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 800)
    # тариф без cache_read → cached по обычной input-ставке
    report = compute_cost(acc, _PRICING, "m")  # _PRICING без cache_read, модель 'm'
    expected = 1000 / 1e6 * 93 + 100 / 1e6 * 295
    assert abs(report.by_currency["₽"] - expected) < 1e-9


def test_real_deepseek_billing_matches_150():
    """Сверка на реальных числах прогона: должно дать 1.50 ₽ (как RouterAI)."""
    acc = UsageAccount()
    # 4 main-вызова из логов: (prompt, completion, cached)
    for prompt, comp, cached in [
        (14543, 136, 0),
        (14722, 200, 14464),
        (15132, 240, 14720),
        (15620, 374, 15104),
    ]:
        acc.add(CATEGORY_MAIN, prompt, comp, cached)
    # finish_gate без кэша
    acc.add("finish_gate", 4571, 1400, 0)
    report = compute_cost(acc, _PRICING_CACHE, "deepseek")
    cost = report.by_currency["₽"]
    assert round(cost, 1) == 1.5, f"got {cost:.4f}"


def test_compute_cost_multi_currency():
    pricing = {
        "enabled": True,
        "currency": "₽",
        "per_tokens": 1_000_000,
        "models": {
            "m": {"input": 93, "output": 295},
            "usd": {"input": 1, "output": 2, "currency": "$", "per_tokens": 1000},
        },
    }
    # Один прогон — одна модель, поэтому multi-currency проверяем на уровне by_currency,
    # имитируя две категории с разными тарифами недоступно через одну модель.
    # Здесь просто проверяем, что разные модели дают разные валюты.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 500)
    r_rub = compute_cost(acc, pricing, "m")
    r_usd = compute_cost(acc, pricing, "usd")
    assert "₽" in r_rub.by_currency
    assert "$" in r_usd.by_currency


# ---------------- report formatting ----------------

def test_format_tokens():
    assert format_tokens(500) == "500"
    assert format_tokens(210456) == "210.5k"
    assert format_tokens(1_240_000) == "1.2M"


def test_format_money_half_up():
    assert format_money(35.05, "₽") == "35.1 ₽"
    assert format_money(0.58, "₽") == "0.6 ₽"
    assert format_money(39.2238, "₽") == "39.2 ₽"


def test_render_with_pricing_and_missing():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 210_500, 38_200)
    acc.add_missing(CATEGORY_SUMMARY)
    report = compute_cost(acc, _PRICING, "m")
    text = render_cost_report(report, model_name="m")
    assert "total" in text
    assert "₽" in text
    assert "не учтены" in text


def test_render_without_pricing_shows_tokens_only():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    report = compute_cost(acc, {"enabled": False}, "m")
    text = render_cost_report(report)
    assert "cost" not in text
    assert "total" in text
