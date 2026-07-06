"""Расчёт стоимости из ``UsageAccount`` + тарифов.

Стоимость считается по модели прогона (`state.default_model`): сейчас все
категории используют одну модель, поэтому тариф один. Структура результата
поддерживает агрегацию по валютам (на случай разных моделей/валют в будущем):
одна валюта → единый total; >1 → сабтоталы без общего total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from config_modules.pricing import ResolvedTariff, resolve_model_tariff
from usage.account import UsageAccount


@dataclass
class CategoryCost:
    category: str
    input: int
    output: int
    cached: int
    calls: int
    missing_usage_calls: int
    cost: Optional[float]      # None → тарифа нет
    currency: Optional[str]


@dataclass
class CostReport:
    categories: list[CategoryCost] = field(default_factory=list)
    # currency -> суммарная стоимость (учтённые категории)
    by_currency: Dict[str, float] = field(default_factory=dict)
    total_input: int = 0
    total_output: int = 0
    total_cached: int = 0
    total_calls: int = 0
    total_missing_usage_calls: int = 0
    has_pricing: bool = False   # тариф найден хотя бы для одной категории

    @property
    def multi_currency(self) -> bool:
        return len(self.by_currency) > 1


def _tariff_cost(
    tariff: ResolvedTariff, input_tokens: int, output_tokens: int, cached_tokens: int
) -> float:
    per = tariff.per_tokens
    cached = max(0, min(cached_tokens, input_tokens))
    if tariff.cache_read is not None:
        fresh = input_tokens - cached
        input_cost = fresh / per * tariff.input + cached / per * tariff.cache_read
    else:
        # Нет cache-ставки → весь input по обычной цене (как раньше).
        input_cost = input_tokens / per * tariff.input
    return input_cost + output_tokens / per * tariff.output


def compute_cost(
    account: UsageAccount,
    pricing: object,
    model_name: str | None,
) -> CostReport:
    """Построить ``CostReport`` по аккаунту и тарифам.

    ``pricing`` — ``PricingConfig`` или сырой dict из config. ``model_name`` —
    модель прогона; если тарифа нет / pricing выключен → стоимость None,
    показываем только токены.
    """
    tariff = resolve_model_tariff(pricing, model_name)
    report = CostReport(has_pricing=tariff is not None)

    # Порядок категорий стабильный: по убыванию суммарных токенов, потом по имени.
    def _sort_key(item):
        cat, bucket = item
        return (-(int(bucket.get("input", 0)) + int(bucket.get("output", 0))), cat)

    for category, bucket in sorted(account.by_category.items(), key=_sort_key):
        inp = int(bucket.get("input", 0) or 0)
        out = int(bucket.get("output", 0) or 0)
        cached = int(bucket.get("cached", 0) or 0)
        calls = int(bucket.get("calls", 0) or 0)
        missing = int(bucket.get("missing_usage_calls", 0) or 0)
        if calls == 0:
            continue
        cost: Optional[float] = None
        currency: Optional[str] = None
        if tariff is not None:
            cost = _tariff_cost(tariff, inp, out, cached)
            currency = tariff.currency
            report.by_currency[currency] = report.by_currency.get(currency, 0.0) + cost
        report.categories.append(
            CategoryCost(
                category=category,
                input=inp,
                output=out,
                cached=cached,
                calls=calls,
                missing_usage_calls=missing,
                cost=cost,
                currency=currency,
            )
        )
        report.total_input += inp
        report.total_output += out
        report.total_cached += cached
        report.total_calls += calls
        report.total_missing_usage_calls += missing

    return report
