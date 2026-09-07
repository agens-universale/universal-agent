"""Расчёт стоимости из ``UsageAccount`` + тарифов.

Стоимость категории строится из двух возможных слагаемых (``CostPart``):
факт от провайдера (биллинг части вызовов) и тарифный расчёт остатка
(вызовы, для которых факта нет). Если для остатка тарифа тоже нет —
категория помечается ``unpriced``, а не молча теряет деньги.

Валюта факта фиксируется в момент вызова (см. ``usage.context``) и хранится
вместе с суммой, поэтому смена провайдера посреди треда не переразмечает
исторические деньги. ``endpoint_url`` здесь нужен только для старых ``meta``,
где валюта не записана. Валюта тарифа — из ``pricing``. Если валюты разошлись,
получаем несколько валют (``multi_currency``) — общий total не считаем,
показываем сабтоталы.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from gc_core.config_modules.pricing import ResolvedTariff, coerce_pricing, resolve_model_tariff
from gc_core.usage.account import LEGACY_CURRENCY, MONEY_FIELD, UsageAccount
from gc_core.usage.currency import provider_currency_for_url


@dataclass(frozen=True)
class CostPart:
    """Одно слагаемое стоимости категории: сумма, валюта и происхождение."""

    amount: float
    currency: str
    source: str          # "provider" | "tariff"


@dataclass
class CategoryCost:
    category: str
    input: int
    output: int
    cached: int
    calls: int
    missing_usage_calls: int
    parts: list[CostPart] = field(default_factory=list)
    unpriced: bool = False      # есть вызовы, цену которых узнать нечем

    @property
    def cost_source(self) -> str:
        sources = {p.source for p in self.parts}
        if sources == {"provider"}:
            return "provider"
        if sources == {"tariff"}:
            return "tariff"
        if sources:
            return "mixed"
        return "none"


@dataclass
class CostReport:
    categories: list[CategoryCost] = field(default_factory=list)
    # currency -> суммарная стоимость (учтённые категории). Порядок ключей —
    # порядок первого появления валюты при обходе категорий (категории идут по
    # убыванию токенов), то есть валюта основной работы оказывается первой.
    # Порядок обязан быть стабильным: по нему строится строка футера.
    by_currency: Dict[str, float] = field(default_factory=dict)
    total_input: int = 0
    total_output: int = 0
    total_cached: int = 0
    total_calls: int = 0
    total_missing_usage_calls: int = 0
    has_cost: bool = False      # есть хотя бы одна учтённая сумма (факт или тариф)
    has_unpriced: bool = False  # есть категория с остатком, цену которого узнать нечем

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
    endpoint_url: str | None = None,
) -> CostReport:
    """Построить ``CostReport`` по аккаунту и тарифам.

    ``pricing`` — ``PricingConfig`` или сырой dict из config. ``model_name`` —
    модель прогона. ``endpoint_url`` — адрес бэкенда (плоский ключ
    ``completions_url``): нужен только для сумм из старых ``meta``, записанных
    версией без валюты. Токены/вызовы по категориям считаются всегда, независимо
    от ``pricing.enabled`` (счётчик usage не зависит от показа денег). Деньги
    считаются только если ``pricing.enabled`` — тогда для каждой категории
    стоимость собирается из факта провайдера (биллённая часть вызовов) и
    тарифного расчёта остальных вызовов (если тариф известен); если и того,
    и другого нет — категория помечается ``unpriced``.
    """
    pricing_cfg = coerce_pricing(pricing)
    tariff = resolve_model_tariff(pricing_cfg, model_name)
    # Валюта для сумм без записанной валюты (старые meta) — по текущему конфигу,
    # то есть ровно как вела себя предыдущая версия. Разбор один на отчёт.
    legacy_currency = provider_currency_for_url(endpoint_url)
    report = CostReport()

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

        parts: list[CostPart] = []
        unpriced = False
        if pricing_cfg.enabled:
            # Денежная сторона — только при включённом pricing (по умолчанию
            # выключен). Токены/вызовы выше считаются в любом случае.
            billed_calls = int(bucket.get("billed_calls", 0) or 0)
            billed_input = int(bucket.get("billed_input", 0) or 0)
            billed_output = int(bucket.get("billed_output", 0) or 0)
            if billed_calls:
                # По части на валюту факта. Суммы без записанной валюты (старые
                # meta) сливаются с legacy-валютой — в одну часть, а не в две
                # одинаковых.
                billed: Dict[str, float] = {}
                for cur, amount in (bucket.get(MONEY_FIELD) or {}).items():
                    key = legacy_currency if cur == LEGACY_CURRENCY else cur
                    billed[key] = billed.get(key, 0.0) + float(amount or 0.0)
                for cur, amount in billed.items():
                    parts.append(CostPart(amount, cur, "provider"))

            rest_in = inp - billed_input
            rest_out = out - billed_output
            rest_cached = cached - int(bucket.get("billed_cached", 0) or 0)
            # Остаток считаем по токенам, а не по вызовам: вызов без usage
            # (``add_missing``) увеличивает ``calls``, не давая ни одного токена —
            # по вызовам он выглядел бы как «остаток есть», хотя тарифить нечего,
            # и создавал бы фантомную нулевую сумму во второй валюте.
            has_rest = rest_in > 0 or rest_out > 0
            if has_rest:
                if tariff is not None:
                    parts.append(CostPart(
                        _tariff_cost(tariff, rest_in, rest_out, rest_cached),
                        tariff.currency, "tariff"))
                else:
                    unpriced = True

            for part in parts:
                report.by_currency[part.currency] = report.by_currency.get(part.currency, 0.0) + part.amount
            report.has_unpriced = report.has_unpriced or unpriced

        report.categories.append(
            CategoryCost(
                category=category,
                input=inp,
                output=out,
                cached=cached,
                calls=calls,
                missing_usage_calls=missing,
                parts=parts,
                unpriced=unpriced,
            )
        )
        report.total_input += inp
        report.total_output += out
        report.total_cached += cached
        report.total_calls += calls
        report.total_missing_usage_calls += missing

    report.has_cost = bool(report.by_currency)
    return report
