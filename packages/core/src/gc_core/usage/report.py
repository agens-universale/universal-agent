"""Рендер итоговой таблицы usage для консоли/логов.

Форматирование чисел:
- деньги — 1 знак после запятой (half-up);
- токены — 1 знак при сокращении k/M, мелкие (<1000) — целым;
- вызовы без usage — строкой-пометкой (итог = нижняя граница).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from gc_core.usage.account import UsageAccount
from gc_core.usage.cost import CostReport, compute_cost


def format_tokens(n: int) -> str:
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def format_money(value: float, currency: str) -> str:
    quantized = Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{quantized} {currency}".strip()


def render_cost_report(report: CostReport, *, model_name: str | None = None, title: str = "Run usage") -> str:
    lines: list[str] = []
    header = title
    if model_name:
        header = f"{title}    model: {model_name}"
    lines.append(header)

    show_cost = report.has_pricing
    if show_cost:
        lines.append(f"{'category':<20}{'input':>10}{'output':>10}{'cost':>12}")
    else:
        lines.append(f"{'category':<20}{'input':>10}{'output':>10}")

    for c in report.categories:
        row = f"{c.category:<20}{format_tokens(c.input):>10}{format_tokens(c.output):>10}"
        if show_cost:
            cost_str = format_money(c.cost, c.currency) if c.cost is not None else "—"
            row += f"{cost_str:>12}"
        lines.append(row)

    lines.append("─" * (52 if show_cost else 40))
    total_row = (
        f"{'total':<20}{format_tokens(report.total_input):>10}"
        f"{format_tokens(report.total_output):>10}"
    )
    if show_cost:
        if report.multi_currency:
            # >1 валюты: единого total нет — выводим сабтоталы отдельной секцией ниже.
            total_row += f"{'—':>12}"
        else:
            cur, amount = next(iter(report.by_currency.items()), (None, 0.0))
            total_row += f"{(format_money(amount, cur) if cur else '—'):>12}"
    lines.append(total_row)

    if show_cost and report.multi_currency:
        lines.append("subtotals by currency:")
        for cur, amount in report.by_currency.items():
            lines.append(f"  {format_money(amount, cur)}")

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

    return "\n".join(lines)


def build_run_usage_report(
    run_usage: UsageAccount,
    pricing: Any,
    model_name: Optional[str],
) -> Optional[str]:
    """Готовый текст отчёта по usage прогона или None, если считать нечего."""
    if not isinstance(run_usage, UsageAccount) or run_usage.is_empty():
        return None
    cost_report = compute_cost(run_usage, pricing, model_name)
    return render_cost_report(cost_report, model_name=model_name, title="Run usage")


def build_run_usage_dict(
    run_usage: UsageAccount,
    pricing: Any,
    model_name: Optional[str],
) -> Optional[dict]:
    """Структурный (JSON-friendly) отчёт по usage прогона или None, если считать нечего.

    Используется для jsonl-события ``run_usage`` (см. ``AgentExecutor.run``), в
    дополнение к текстовому отчёту ``build_run_usage_report``.
    """
    if not isinstance(run_usage, UsageAccount) or run_usage.is_empty():
        return None
    report = compute_cost(run_usage, pricing, model_name)

    categories: dict[str, dict[str, Any]] = {}
    for c in report.categories:
        entry: dict[str, Any] = {"input": c.input, "output": c.output}
        if c.cached:
            entry["cached"] = c.cached
        if c.missing_usage_calls:
            entry["missing_usage_calls"] = c.missing_usage_calls
        if report.has_pricing and c.cost is not None:
            entry["cost"] = c.cost
            entry["currency"] = c.currency
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
    if report.has_pricing:
        out["cost"] = {"by_currency": dict(report.by_currency)}
    return out
