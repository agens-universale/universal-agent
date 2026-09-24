from gc_core.usage.account import (
    CATEGORY_FINISH_GATE,
    CATEGORY_MAIN,
    CATEGORY_SUBAGENT,
    CATEGORY_SUMMARY,
    LEGACY_CURRENCY,
    MONEY_FIELD,
    UsageAccount,
    category_for_request_label,
)
from gc_core.usage.context import (
    current_usage_context,
    record_usage,
    usage_scope,
)
from gc_core.usage.cost import CostPart, compute_cost
from gc_core.usage.normalize import normalize_usage
from gc_core.usage.report import (
    format_money,
    format_tokens,
    render_cost_report,
)

# ---------------- normalize ----------------


def test_normalize_chat_completions_shape():
    s = normalize_usage({"prompt_tokens": 100, "completion_tokens": 20})
    assert (s.input, s.output, s.cached) == (100, 20, 0)
    assert s.cost is None
    assert s.billed is False


def test_normalize_responses_shape():
    s = normalize_usage({"input_tokens": 5, "output_tokens": 7})
    assert (s.input, s.output, s.cached) == (5, 7, 0)
    assert s.cost is None
    assert s.billed is False


def test_normalize_missing_returns_none():
    assert normalize_usage(None) is None
    assert normalize_usage({}) is None
    assert normalize_usage({"foo": "bar"}) is None
    assert normalize_usage("garbage") is None
    assert normalize_usage({"prompt_tokens": "x"}) is None


def test_normalize_partial_fills_zero():
    s1 = normalize_usage({"prompt_tokens": 10})
    assert (s1.input, s1.output, s1.cached) == (10, 0, 0)
    s2 = normalize_usage({"completion_tokens": 3})
    assert (s2.input, s2.output, s2.cached) == (0, 3, 0)


def test_normalize_cached_openai_style():
    # RouterAI/OpenAI: prompt_tokens_details.cached_tokens
    u = {
        "prompt_tokens": 14722,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 14464},
    }
    s = normalize_usage(u)
    assert (s.input, s.output, s.cached) == (14722, 50, 14464)


def test_normalize_cached_deepseek_native():
    # DeepSeek-native: prompt_cache_hit_tokens
    u = {
        "prompt_tokens": 1000,
        "completion_tokens": 10,
        "prompt_cache_hit_tokens": 800,
    }
    s = normalize_usage(u)
    assert (s.input, s.output, s.cached) == (1000, 10, 800)


def test_normalize_cached_clamped_to_input():
    u = {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 999},
    }
    s = normalize_usage(u)
    assert (s.input, s.output, s.cached) == (
        100,
        5,
        100,
    )  # cached не превышает input


def test_normalize_no_cache_details_zero():
    u = {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "prompt_tokens_details": None,
    }
    s = normalize_usage(u)
    assert (s.input, s.output, s.cached) == (100, 5, 0)


def test_normalize_extracts_cost():
    s = normalize_usage(
        {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.0058}
    )
    assert s.cost == 0.0058
    assert s.billed is True


def test_normalize_zero_cost_is_a_fact():
    # Бесплатная модель: 0 — это факт «бесплатно», а не «цена неизвестна».
    s = normalize_usage(
        {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0}
    )
    assert s.cost == 0.0
    assert s.billed is True


def test_normalize_without_cost_is_not_billed():
    s = normalize_usage({"prompt_tokens": 100, "completion_tokens": 10})
    assert s.cost is None
    assert s.billed is False


def test_normalize_rejects_broken_cost():
    for bad in ("0.5", -1, True, float("inf"), None):
        s = normalize_usage(
            {"prompt_tokens": 100, "completion_tokens": 10, "cost": bad}
        )
        assert s.billed is False, bad
        assert s.cost is None, bad


def test_normalize_byok_is_not_billed():
    # При BYOK неизвестно, комиссия в cost или полная цена — факту не доверяем.
    s = normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cost": 0.0058,
            "is_byok": True,
        }
    )
    assert s.billed is False
    assert s.cost is None


def test_normalize_byok_string_true_is_not_billed():
    # Провайдер может прислать is_byok строкой ("true"), а не bool — guard должен
    # быть устойчив к обеим формам.
    s = normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cost": 0.0058,
            "is_byok": "true",
        }
    )
    assert s.billed is False
    assert s.cost is None


def test_normalize_indexing_still_works():
    s = normalize_usage(
        {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.5}
    )
    assert (s[0], s[1], s[2]) == (100, 10, 0)


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


def test_add_records_billed_shelf():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.5, billed=True, currency="$")
    acc.add(CATEGORY_MAIN, 2000, 200)  # без цены
    b = acc.by_category[CATEGORY_MAIN]
    assert b["calls"] == 2
    assert b["billed_calls"] == 1
    assert b["input"] == 3000
    assert b["billed_input"] == 1000
    assert b[MONEY_FIELD] == {"$": 0.5}


def test_add_without_currency_lands_in_unknown_key():
    # Валюту знает транспорт; вызыватель без неё (тесты, orphan-путь) пишет в
    # «валюта неизвестна» — она разрешается при показе по текущему конфигу.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.5, billed=True)
    assert acc.by_category[CATEGORY_MAIN][MONEY_FIELD] == {
        LEGACY_CURRENCY: 0.5
    }


def test_add_keeps_currencies_apart():
    # Ядро фичи: рубли не складываются с долларами даже внутри одной категории.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=2.0, billed=True, currency="$")
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=200.0, billed=True, currency="₽")
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=1.0, billed=True, currency="$")
    assert acc.by_category[CATEGORY_MAIN][MONEY_FIELD] == {
        "$": 3.0,
        "₽": 200.0,
    }


def test_zero_cost_counts_as_billed():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 10, 0, cost=0.0, billed=True, currency="$")
    b = acc.by_category[CATEGORY_MAIN]
    assert b["billed_calls"] == 1
    assert b[MONEY_FIELD] == {"$": 0.0}


def test_cost_survives_meta_roundtrip():
    acc = UsageAccount()
    acc.add(
        CATEGORY_MAIN, 1000, 100, 0, cost=0.0058, billed=True, currency="₽"
    )
    restored = UsageAccount.from_meta(acc.to_meta())
    assert restored.by_category[CATEGORY_MAIN][MONEY_FIELD] == {"₽": 0.0058}
    assert restored.by_category[CATEGORY_MAIN]["billed_calls"] == 1


def test_to_meta_snapshot_is_detached():
    # Снимок не должен продолжать меняться вслед за накопителем.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=1.0, billed=True, currency="$")
    snapshot = acc.to_meta()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=5.0, billed=True, currency="$")
    assert snapshot["by_category"][CATEGORY_MAIN][MONEY_FIELD] == {"$": 1.0}


def test_old_meta_without_new_fields_reads_as_unbilled():
    old = {
        "by_category": {
            "main": {"input": 100, "output": 10, "cached": 0, "calls": 1}
        }
    }
    acc = UsageAccount.from_meta(old)
    b = acc.by_category["main"]
    assert b["billed_calls"] == 0
    assert b[MONEY_FIELD] == {}


def test_legacy_meta_cost_reads_as_unknown_currency():
    # meta, записанные версией без валюты: голое число ложится в «неизвестна».
    old = {
        "by_category": {
            "main": {
                "input": 1000,
                "output": 100,
                "calls": 1,
                "billed_calls": 1,
                "billed_input": 1000,
                "billed_output": 100,
                "cost": 0.75,
            }
        }
    }
    acc = UsageAccount.from_meta(old)
    assert acc.by_category["main"][MONEY_FIELD] == {LEGACY_CURRENCY: 0.75}


def test_merge_carries_cost_and_shelf():
    parent, child = UsageAccount(), UsageAccount()
    child.add(
        CATEGORY_MAIN, 1000, 100, 0, cost=0.25, billed=True, currency="$"
    )
    parent.merge(child, as_category=CATEGORY_SUBAGENT)
    b = parent.by_category[CATEGORY_SUBAGENT]
    assert b[MONEY_FIELD] == {"$": 0.25}
    assert b["billed_input"] == 1000


def test_merge_keeps_currencies_apart():
    # Субагент ходил на другой эндпоинт — его валюта не сливается с родительской.
    parent, child = UsageAccount(), UsageAccount()
    parent.add(
        CATEGORY_MAIN, 1000, 100, 0, cost=2.0, billed=True, currency="$"
    )
    child.add(
        CATEGORY_MAIN, 1000, 100, 0, cost=200.0, billed=True, currency="₽"
    )
    child.add(CATEGORY_MAIN, 1000, 100, 0, cost=1.0, billed=True, currency="$")
    parent.merge(child, as_category=CATEGORY_SUBAGENT)
    assert parent.by_category[CATEGORY_SUBAGENT][MONEY_FIELD] == {
        "₽": 200.0,
        "$": 1.0,
    }
    assert parent.by_category[CATEGORY_MAIN][MONEY_FIELD] == {"$": 2.0}


def test_delta_from_subtracts_cost():
    base = UsageAccount()
    base.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.25, billed=True, currency="$")
    now = UsageAccount.from_meta(base.to_meta())
    now.add(CATEGORY_MAIN, 500, 50, 0, cost=0.10, billed=True, currency="$")
    delta = now.delta_from(base)
    assert delta.by_category[CATEGORY_MAIN]["input"] == 500
    assert (
        abs(delta.by_category[CATEGORY_MAIN][MONEY_FIELD]["$"] - 0.10) < 1e-12
    )


def test_delta_from_subtracts_each_currency():
    base = UsageAccount()
    base.add(CATEGORY_MAIN, 1000, 100, 0, cost=2.0, billed=True, currency="$")
    base.add(
        CATEGORY_MAIN, 1000, 100, 0, cost=200.0, billed=True, currency="₽"
    )
    now = UsageAccount.from_meta(base.to_meta())
    now.add(CATEGORY_MAIN, 500, 50, 0, cost=50.0, billed=True, currency="₽")
    delta = now.delta_from(base)
    # Доллары не изменились — их в дельте прогона быть не должно.
    assert delta.by_category[CATEGORY_MAIN][MONEY_FIELD] == {"₽": 50.0}


def test_delta_epsilon_does_not_create_category():
    # Float-хвост вида 1.7e-17 не должен порождать категорию: иначе она создаёт
    # валюту, и в футере появляется фантомное второе слагаемое.
    base = UsageAccount()
    base.add(
        CATEGORY_MAIN, 1000, 100, 0, cost=0.1 + 0.2, billed=True, currency="$"
    )
    now = UsageAccount.from_meta(base.to_meta())
    now.by_category[CATEGORY_MAIN][MONEY_FIELD]["$"] = (
        0.3  # ноль с точностью до эпсилон
    )
    assert now.delta_from(base).is_empty()


def test_delta_epsilon_applies_per_currency():
    # Эпсилон на каждую валюту: хвост в одной не должен всплыть, пока другая растёт.
    base = UsageAccount()
    base.add(
        CATEGORY_MAIN, 1000, 100, 0, cost=0.1 + 0.2, billed=True, currency="$"
    )
    base.add(
        CATEGORY_MAIN, 1000, 100, 0, cost=100.0, billed=True, currency="₽"
    )
    now = UsageAccount.from_meta(base.to_meta())
    now.by_category[CATEGORY_MAIN][MONEY_FIELD]["$"] = 0.3
    now.add(CATEGORY_MAIN, 500, 50, 0, cost=25.0, billed=True, currency="₽")
    assert now.delta_from(base).by_category[CATEGORY_MAIN][MONEY_FIELD] == {
        "₽": 25.0
    }


def test_totals_sums_each_currency():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 10, 0, cost=0.75, billed=True, currency="$")
    acc.add(CATEGORY_SUMMARY, 100, 10, 0, cost=0.25, billed=True, currency="$")
    acc.add(CATEGORY_SUMMARY, 100, 10, 0, cost=40.0, billed=True, currency="₽")
    assert acc.totals()[MONEY_FIELD] == {"$": 1.0, "₽": 40.0}


def test_label_to_category():
    assert category_for_request_label("summary_batch_1") == CATEGORY_SUMMARY
    assert (
        category_for_request_label("intent_guard_finish_gate")
        == CATEGORY_FINISH_GATE
    )
    assert (
        category_for_request_label("command_moderation_judge")
        == "command_moderation"
    )
    assert (
        category_for_request_label("project_memory_compaction")
        == "project_memory"
    )
    assert category_for_request_label("intent_scope_summary") == "intent_scope"
    assert category_for_request_label("") == CATEGORY_MAIN
    assert category_for_request_label("whatever") == CATEGORY_MAIN


def test_label_to_category_subagent_prefix():
    """Регресс (ревью Task 7): вспомогательные LLM-вызовы субагентов (напр.
    special_agent role generation, request_label="subagent_special_agent") должны
    падать в категорию subagent, не main — request_chat_completion_text резолвит
    категорию именно через этот маппер (usage/account.py)."""
    assert (
        category_for_request_label("subagent_special_agent")
        == CATEGORY_SUBAGENT
    )


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
    assert report.has_cost is True
    # 250000/1e6*93 + 40000/1e6*295 = 23.25 + 11.8 = 35.05
    assert abs(report.by_currency["₽"] - 35.05) < 1e-6
    assert not report.multi_currency


def test_compute_cost_no_tariff():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    report = compute_cost(acc, _PRICING, "unknown")
    assert report.has_cost is False
    assert report.categories[0].parts == []
    assert report.categories[0].unpriced is True


def test_compute_cost_disabled():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 100, 20)
    report = compute_cost(acc, {**_PRICING, "enabled": False}, "m")
    assert report.has_cost is False


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
    report = compute_cost(
        acc, _PRICING, "m"
    )  # _PRICING без cache_read, модель 'm'
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
            "usd": {
                "input": 1,
                "output": 2,
                "currency": "$",
                "per_tokens": 1000,
            },
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


_PRICING_RUB = {
    "enabled": True,
    "currency": "₽",
    "per_tokens": 1_000_000,
    "input": 100,
    "output": 200,
}
_PRICING_OFF = {
    "enabled": False,
    "currency": "₽",
    "per_tokens": 1_000_000,
    "input": 100,
    "output": 200,
}
_PRICING_NO_RATES = {"enabled": True, "currency": "₽", "per_tokens": 1_000_000}


def test_fully_billed_category_uses_fact():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=7.5, billed=True)
    r = compute_cost(acc, _PRICING_RUB, "m")
    cat = r.categories[0]
    assert cat.cost_source == "provider"
    assert cat.parts == [CostPart(7.5, "$", "provider")]
    assert r.by_currency == {"$": 7.5}


def test_unbilled_category_uses_tariff():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000)
    r = compute_cost(acc, _PRICING_RUB, "m")
    cat = r.categories[0]
    assert cat.cost_source == "tariff"
    assert cat.parts == [CostPart(120.0, "₽", "tariff")]  # 1M*100 + 0.1M*200


def test_mixed_category_splits_fact_and_tariff():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=7.5, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)  # без цены
    r = compute_cost(acc, _PRICING_RUB, "m")
    cat = r.categories[0]
    assert cat.cost_source == "mixed"
    assert CostPart(7.5, "$", "provider") in cat.parts
    assert CostPart(60.0, "₽", "tariff") in cat.parts  # 0.5M*100 + 0.05M*200
    assert r.multi_currency is True


def test_mixed_without_tariff_is_marked_unpriced():
    # Дефолтный конфиг: ставок нет. Остаток без цены не должен молча пропадать.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=7.5, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    r = compute_cost(acc, _PRICING_NO_RATES, "m")
    cat = r.categories[0]
    assert cat.parts == [CostPart(7.5, "$", "provider")]
    assert cat.unpriced is True
    assert r.has_unpriced is True


def test_has_cost_true_on_default_config_with_facts():
    # Ставок нет, но провайдер вернул цену — деньги показываем.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.5, billed=True)
    assert compute_cost(acc, _PRICING_NO_RATES, "m").has_cost is True


def test_pricing_disabled_hides_everything():
    # "Всё" здесь — деньги; токены при выключенном pricing остаются (см.
    # test_disabled_pricing_keeps_tokens).
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.5, billed=True)
    r = compute_cost(acc, _PRICING_OFF, "m")
    assert r.has_cost is False
    assert r.by_currency == {}
    assert r.categories[0].parts == []
    assert r.categories[0].unpriced is False


def test_disabled_pricing_keeps_tokens():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.5, billed=True)
    r = compute_cost(acc, _PRICING_OFF, "m")
    assert r.has_cost is False
    assert r.by_currency == {}
    assert [c.category for c in r.categories] == [CATEGORY_MAIN]
    assert r.total_input == 1000
    assert r.total_output == 100
    assert r.categories[0].parts == []


def test_disabled_pricing_keeps_missing_calls():
    acc = UsageAccount()
    acc.add_missing(CATEGORY_MAIN)
    r = compute_cost(acc, _PRICING_OFF, "m")
    assert r.total_calls == 1
    assert r.total_missing_usage_calls == 1


def test_empty_part_does_not_create_currency():
    # Все вызовы фактические → тарифного остатка нет → рублёвой валюты не существует.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.5, billed=True)
    r = compute_cost(acc, _PRICING_RUB, "m")
    assert r.by_currency == {"$": 0.5}
    assert r.multi_currency is False


def test_usage_less_call_does_not_create_phantom_currency():
    # Вызов без usage (add_missing) не даёт токенов → не должен создавать
    # тарифный остаток и фантомную вторую валюту с нулевой суммой.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=7.5, billed=True)
    acc.add_missing(CATEGORY_MAIN)
    r = compute_cost(acc, _PRICING_RUB, "m")
    assert r.by_currency == {"$": 7.5}
    assert r.multi_currency is False
    assert r.categories[0].cost_source == "provider"


def test_tariff_remainder_accounts_for_cached():
    pricing = dict(_PRICING_RUB, cache_read=10)
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 0, 1_000_000, cost=1.0, billed=True)
    acc.add(CATEGORY_MAIN, 1_000_000, 0, 400_000)
    r = compute_cost(acc, pricing, "m")
    # Остаток: input 1M, из них cached 0.4M по 10, остальные 0.6M по 100.
    assert CostPart(64.0, "₽", "tariff") in r.categories[0].parts


# ---------------- валюта факта по эндпоинту ----------------

_RU_URL = "https://routerai.ru/api/v1/chat/completions"
_USD_URL = "https://openrouter.ai/api/v1/chat/completions"


def test_fact_currency_defaults_to_dollars_without_url():
    # Гард обратной совместимости: трёхаргументный вызов работает как раньше.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=7.5, billed=True)
    assert compute_cost(acc, _PRICING_RUB, "m").by_currency == {"$": 7.5}


def test_fact_currency_follows_ru_endpoint():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=7.5, billed=True)
    r = compute_cost(acc, _PRICING_RUB, "m", _RU_URL)
    assert r.categories[0].parts == [CostPart(7.5, "₽", "provider")]
    assert r.by_currency == {"₽": 7.5}


def test_ru_endpoint_and_rub_tariff_collapse_into_one_currency():
    # Ради этого случая правило и вводилось: факт и тариф сходятся в рублях,
    # а не расходятся на два подытога с маркером неполноты.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=7.5, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    r = compute_cost(acc, _PRICING_RUB, "m", _RU_URL)
    assert set(r.by_currency) == {"₽"}
    assert r.multi_currency is False
    assert r.has_unpriced is False
    assert r.categories[0].cost_source == "mixed"


def test_ru_endpoint_with_usd_tariff_still_splits():
    # Если тариф задан в долларах, валюты по-прежнему две — это не регресс.
    pricing = {
        "enabled": True,
        "currency": "₽",
        "per_tokens": 1_000_000,
        "models": {"m": {"input": 100, "output": 200, "currency": "$"}},
    }
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=7.5, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    r = compute_cost(acc, pricing, "m", _RU_URL)
    assert set(r.by_currency) == {"₽", "$"}
    assert r.multi_currency is True


def test_non_ru_endpoint_and_garbage_url_give_dollars():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=7.5, billed=True)
    assert compute_cost(acc, _PRICING_RUB, "m", _USD_URL).by_currency == {
        "$": 7.5
    }
    assert compute_cost(acc, _PRICING_RUB, "m", "мусор").by_currency == {
        "$": 7.5
    }
    assert compute_cost(acc, _PRICING_RUB, "m", None).by_currency == {"$": 7.5}


def test_disabled_pricing_with_ru_endpoint_shows_no_money():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=7.5, billed=True)
    r = compute_cost(acc, _PRICING_OFF, "m", _RU_URL)
    assert r.has_cost is False
    assert r.by_currency == {}
    assert r.total_input == 1000


def test_ru_endpoint_report_has_single_total_without_subtotals():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=7.5, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    text = render_cost_report(
        compute_cost(acc, _PRICING_RUB, "m", _RU_URL), model_name="m"
    )
    assert "subtotals by currency" not in text
    assert "$" not in text
    assert "~" in text  # обе стороны есть — тильда на месте


# ---------------- report formatting ----------------


def test_format_tokens():
    assert format_tokens(500) == "500"
    assert format_tokens(210456) == "210.5k"
    assert format_tokens(1_240_000) == "1.2M"


def test_format_money_two_digits_below_ten():
    assert format_money(0.58, "₽") == "0.58 ₽"
    assert format_money(4.3, "$") == "4.30 $"
    assert format_money(0.038945, "$") == "0.04 $"


def test_format_money_one_digit_from_ten():
    assert format_money(35.05, "₽") == "35.1 ₽"
    assert format_money(10.45, "$") == "10.5 $"
    assert format_money(39.2238, "₽") == "39.2 ₽"


def test_format_money_threshold_uses_rounded_value():
    # 9.996 округляется до 10.0 → формат крупного числа, один знак.
    assert format_money(9.996, "$") == "10.0 $"


def test_format_money_tiny_nonzero():
    assert format_money(0.000588, "$") == "<0.01 $"
    assert format_money(0.0, "$") == "0.00 $"


def test_report_no_tilde_when_only_tariff():
    # Чисто тарифный отчёт (нет факта провайдера нигде) — тильды нет: она ничего
    # не сообщает, когда сторона всего одна.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000)
    text = render_cost_report(
        compute_cost(acc, _PRICING_RUB, "m"), model_name="m"
    )
    assert "120.0 ₽" in text
    assert "~" not in text


def test_report_marks_tariff_part_with_tilde_when_mixed():
    # Реальное смешение (одна категория факт, другая тариф) — тильда нужна и есть.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=7.5, billed=True)
    acc.add(CATEGORY_FINISH_GATE, 1_000_000, 100_000)
    text = render_cost_report(
        compute_cost(acc, _PRICING_RUB, "m"), model_name="m"
    )
    assert "~120.0 ₽" in text
    assert "~ — расчёт по тарифу" in text


def test_report_category_without_any_price_shows_dash_not_dash_question():
    # Категория совсем без цены (нет ни факта, ни тарифа) — «—» в ячейке, без «?»:
    # «?» переезжает на строку total и в легенду («—?» читается странно).
    acc = UsageAccount()
    acc.add(
        CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=7.5, billed=True
    )  # факт
    acc.add(CATEGORY_FINISH_GATE, 1000, 100)  # без цены вовсе
    text = render_cost_report(
        compute_cost(acc, _PRICING_NO_RATES, "m"), model_name="m"
    )
    lines = {
        line.split()[0]: line for line in text.splitlines() if line.strip()
    }
    assert lines["finish_gate"].rstrip().endswith("—")
    assert "total" in text and "?" in text  # неполнота видна на итоге


def test_subtotal_label_reflects_sources_not_currency():
    # Метка подытога — из фактических источников частей в этой валюте, а не по
    # самой валюте: совпадение валюты с источником случайно и ломается, как только
    # тариф задан в той же валюте, что и факт. Собираем CostReport вручную, чтобы
    # проверить рендер независимо от того, может ли compute_cost сегодня
    # породить такую валютную корзину.
    from gc_core.usage.cost import CategoryCost, CostReport

    report = CostReport(
        categories=[
            CategoryCost(
                category="main",
                input=1_000_000,
                output=100_000,
                cached=0,
                calls=1,
                missing_usage_calls=0,
                parts=[CostPart(7.5, "$", "provider")],
            ),
            CategoryCost(
                category="other",
                input=500_000,
                output=50_000,
                cached=0,
                calls=1,
                missing_usage_calls=0,
                parts=[CostPart(60.0, "$", "tariff")],
            ),
            CategoryCost(
                category="finish_gate",
                input=200_000,
                output=20_000,
                cached=0,
                calls=1,
                missing_usage_calls=0,
                parts=[CostPart(24.0, "₽", "tariff")],
            ),
        ],
        by_currency={"$": 67.5, "₽": 24.0},
        total_input=1_700_000,
        total_output=170_000,
        total_calls=3,
        has_cost=True,
    )
    text = render_cost_report(report, model_name="m")
    assert "67.5 $ (факт + расчёт)" in text
    assert "24.0 ₽ (расчёт по тарифу)" in text


def test_report_marks_unpriced_with_question():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.5, billed=True)
    acc.add(CATEGORY_MAIN, 1000, 100)
    text = render_cost_report(
        compute_cost(acc, _PRICING_NO_RATES, "m"), model_name="m"
    )
    assert "?" in text
    assert "итог неполный" in text


def test_report_joins_currencies_with_plus():
    # «+» в ячейке категории — соединитель слагаемых, а не признак неполноты.
    acc = UsageAccount()
    acc.add(
        CATEGORY_MAIN,
        1_000_000,
        100_000,
        0,
        cost=2.0,
        billed=True,
        currency="$",
    )
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000)  # остаток по рублёвому тарифу
    text = render_cost_report(
        compute_cost(acc, _PRICING_RUB, "m"), model_name="m"
    )
    main = next(line for line in text.splitlines() if line.startswith("main"))
    assert "2.00 $+~" in main
    assert "?" not in text  # неполноты здесь нет


def test_render_with_pricing_and_missing():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 210_500, 38_200)
    acc.add_missing(
        CATEGORY_SUMMARY
    )  # вызов без usage — токенов нет, цену узнать нечем
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


def test_report_without_cost_column_has_no_legends():
    # Тарифа нет, фактической цены нет: только токены, без маркеров и легенд.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100)
    pricing = {
        "enabled": True,
        "currency": "₽",
        "per_tokens": 1_000_000,
        "input": 0,
        "output": 0,
    }
    text = render_cost_report(compute_cost(acc, pricing, "m"), model_name="m")
    assert "cost" not in text
    assert "~ —" not in text
    assert "+ —" not in text
