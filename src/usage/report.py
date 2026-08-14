"""Рендер итоговой таблицы usage для консоли/логов.

Форматирование чисел:
- деньги — до 10: два знака после запятой, от 10: один (half-up по округлённому);
- токены — 1 знак при сокращении k/M, мелкие (<1000) — целым;
- вызовы без usage — строкой-пометкой (итог = нижняя граница).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from usage.account import UsageAccount
from usage.cost import CostPart, CostReport, compute_cost


def format_tokens(n: int) -> str:
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def format_money(value: float, currency: str) -> str:
    """Денежная сумма: до 10 — два знака, от 10 — один.

    Порог применяется к округлённому значению (9.996 → «10.0»). Ненулевая сумма,
    схлопывающаяся в ноль, печатается как «<0.01»: «0.00» читалось бы как
    «бесплатно», а это неправда.
    """
    amount = Decimal(str(value))
    coarse = amount.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if abs(coarse) >= 10:
        return f"{coarse} {currency}".strip()
    fine = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if fine == 0 and value > 0:
        return f"<0.01 {currency}".strip()
    return f"{fine} {currency}".strip()


def format_parts(parts: list[CostPart], unpriced: bool = False, mark_tariff: bool = False) -> str:
    """Ячейка стоимости: части через «+», «?» при неполноте.

    Знаки те же, что в футере TUI: «+» соединяет слагаемые суммы, «?» — есть
    трата, стоимость которой узнать неоткуда.

    ``mark_tariff`` — ставить тильду перед тарифной частью; включается вызывающим
    только когда в ОТЧЁТЕ целиком есть обе стороны (факт и тариф) — иначе тильда
    ничего не сообщает (см. ``render_cost_report``). Категория совсем без цены
    (``parts`` пуст) рендерится как «—» без «?»: «?» переезжает на строку total
    и в легенду — «—?» читается странно."""
    if not parts:
        return "—"
    chunks = []
    for part in parts:
        money = format_money(part.amount, part.currency)
        chunks.append(f"~{money}" if mark_tariff and part.source == "tariff" else money)
    text = "+".join(chunks)
    return f"{text}?" if unpriced else text


def render_cost_report(report: CostReport, *, model_name: str | None = None, title: str = "Run usage") -> str:
    lines: list[str] = []
    header = title
    if model_name:
        header = f"{title}    model: {model_name}"
    lines.append(header)

    show_cost = report.has_cost
    if show_cost:
        lines.append(f"{'category':<20}{'input':>10}{'output':>10}{'cost':>22}")
    else:
        lines.append(f"{'category':<20}{'input':>10}{'output':>10}")

    # Тильда осмысленна только когда в отчёте есть ОБЕ стороны — факт и тариф;
    # чисто тарифный (или чисто фактический) отчёт её не ставит нигде.
    has_provider_part = any(p.source == "provider" for c in report.categories for p in c.parts)
    has_tariff_part = any(p.source == "tariff" for c in report.categories for p in c.parts)
    mark_tariff = has_provider_part and has_tariff_part

    for c in report.categories:
        row = f"{c.category:<20}{format_tokens(c.input):>10}{format_tokens(c.output):>10}"
        if show_cost:
            row += f"{format_parts(c.parts, c.unpriced, mark_tariff):>22}"
        lines.append(row)

    lines.append("─" * (62 if show_cost else 40))
    total_row = (
        f"{'total':<20}{format_tokens(report.total_input):>10}"
        f"{format_tokens(report.total_output):>10}"
    )
    if show_cost:
        suffix = "?" if report.has_unpriced else ""
        if report.multi_currency:
            # >1 валюты: единого total нет — выводим сабтоталы отдельной секцией ниже.
            total_row += f"{'—' + suffix:>22}"
        else:
            cur, amount = next(iter(report.by_currency.items()), (None, 0.0))
            cell = (format_money(amount, cur) if cur else "—") + suffix
            total_row += f"{cell:>22}"
    lines.append(total_row)

    if show_cost and report.multi_currency:
        # Метка подытога — из фактических источников частей, попавших в эту
        # валюту, а не из самой валюты (валюта факта и валюта тарифа могут совпасть).
        sources_by_currency: dict[str, set] = {}
        for c in report.categories:
            for p in c.parts:
                sources_by_currency.setdefault(p.currency, set()).add(p.source)
        lines.append("subtotals by currency:")
        for cur, amount in report.by_currency.items():
            sources = sources_by_currency.get(cur, set())
            if sources == {"provider"}:
                label = "факт провайдера"
            elif sources == {"tariff"}:
                label = "расчёт по тарифу"
            else:
                label = "факт + расчёт"
            lines.append(f"  {format_money(amount, cur)} ({label})")

    if report.total_cached > 0:
        share = report.total_cached / report.total_input * 100 if report.total_input else 0
        lines.append(
            f"из них cached input: {format_tokens(report.total_cached)} "
            f"({share:.0f}% — по cache-ставке)"
        )

    if report.total_missing_usage_calls > 0:
        lines.append(
            f"⚠ {report.total_missing_usage_calls} вызов(ов) без usage не учтены "
            f"(итог — нижняя граница)"
        )

    if show_cost:
        if mark_tariff:
            lines.append("~ — расчёт по тарифу")
        if report.has_unpriced:
            lines.append("? — часть вызовов без цены, итог неполный")

    return "\n".join(lines)


def build_run_usage_report(
    run_usage: UsageAccount,
    pricing: Any,
    model_name: Optional[str],
    endpoint_url: Optional[str] = None,
) -> Optional[str]:
    """Готовый текст отчёта по usage прогона или None, если считать нечего.

    ``endpoint_url`` — адрес бэкенда: из него выводится валюта фактической цены.
    """
    if not isinstance(run_usage, UsageAccount) or run_usage.is_empty():
        return None
    cost_report = compute_cost(run_usage, pricing, model_name, endpoint_url)
    return render_cost_report(cost_report, model_name=model_name, title="Run usage")


def build_run_usage_dict(
    run_usage: UsageAccount,
    pricing: Any,
    model_name: Optional[str],
    endpoint_url: Optional[str] = None,
) -> Optional[dict]:
    """Структурный (JSON-friendly) отчёт по usage прогона или None, если считать нечего.

    Используется для jsonl-события ``run_usage`` (см. ``AgentExecutor.run``), в
    дополнение к текстовому отчёту ``build_run_usage_report``.
    """
    if not isinstance(run_usage, UsageAccount) or run_usage.is_empty():
        return None
    report = compute_cost(run_usage, pricing, model_name, endpoint_url)

    categories: dict[str, dict[str, Any]] = {}
    for c in report.categories:
        entry: dict[str, Any] = {"input": c.input, "output": c.output}
        if c.cached:
            entry["cached"] = c.cached
        if c.missing_usage_calls:
            entry["missing_usage_calls"] = c.missing_usage_calls
        if report.has_cost:
            entry["cost_source"] = c.cost_source
            entry["cost_parts"] = [
                {"amount": p.amount, "currency": p.currency, "source": p.source} for p in c.parts
            ]
            entry["unpriced"] = c.unpriced
            if len(c.parts) == 1:
                entry["cost"] = c.parts[0].amount
                entry["currency"] = c.parts[0].currency
        categories[c.category] = entry

    total: dict[str, Any] = {"input": report.total_input, "output": report.total_output}
    if report.total_cached:
        total["cached"] = report.total_cached
    if report.total_missing_usage_calls:
        total["missing_usage_calls"] = report.total_missing_usage_calls

    out: dict[str, Any] = {
        "model": model_name,
        "categories": categories,
        "total": total,
    }
    if report.has_cost:
        out["cost"] = {"by_currency": dict(report.by_currency)}
    return out
