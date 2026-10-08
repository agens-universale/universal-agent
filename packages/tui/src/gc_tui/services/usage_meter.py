"""Индикатор токенов/стоимости запроса и сессии для шапки TUI (чистая презентация).

Формат: «запрос <tok>/<cost> · сессия <tok>/<cost>». Источник — снапшоты
``UsageAccount.to_meta()`` (in-memory state исполнителя), тариф — секция ``pricing``.

Правила видимости:
- сессия пуста (провайдер не вернул токенов) → ``None`` (весь блок скрыт);
- тариф выключен / не задан → только токены, без цены (как в консольном отчёте);
- итога запроса ещё нет (первый ход в процессе) → только сегмент сессии.
"""

from __future__ import annotations

from typing import Any, Optional

from gc_core.usage.account import UsageAccount
from gc_core.usage.cost import CostReport, compute_cost
from gc_core.usage.report import format_money, format_tokens
from rich.text import Text

from gc_tui.palette import DIM, INK

# Стиль меток/чисел подчинён модели в футере: метка приглушена, число — ink.
_LABEL = DIM
_VALUE = INK
# Разделитель сегментов футера (тот же, что у модель/контекст).
_SEP = " · "


def _cost_display(report: CostReport, *, compact: bool) -> Optional[str]:
    """Строка стоимости («2.00$+200.0₽~?») или None, если денег в отчёте нет.

    Показываются ВСЕ валюты, ни одна не сворачивается: доля токенов не
    пропорциональна деньгам, поэтому свести картину к одной цифре значит скрыть
    неизвестно какую часть суммы — основная могла оказаться как раз в спрятанной
    валюте. Порядок валют задаёт отчёт (см. ``CostReport.by_currency``).

    Знаки: «+» соединяет части суммы; «~» — часть суммы посчитана по тарифу, а не
    измерена (ставится, только когда в отчёте есть ОБЕ стороны — иначе не сообщает
    ничего); «?» — есть трата, стоимость которой узнать неоткуда.

    ``compact`` — формат футера, без пробела перед валютой («0.5₽», не «0.5 ₽»).
    """
    if not report.has_cost:
        return None
    has_provider = any(
        p.source == "provider" for c in report.categories for p in c.parts
    )
    has_tariff = any(
        p.source == "tariff" for c in report.categories for p in c.parts
    )
    marks = ""
    if has_provider and has_tariff:
        marks += "~"
    if report.has_unpriced:
        marks += "?"
    chunks = []
    for currency, amount in report.by_currency.items():
        money = format_money(amount, currency)
        chunks.append(money.replace(" ", "") if compact else money)
    return "+".join(chunks) + marks


def format_nonzero_cost(
    meta: Any, pricing: Any, model: str | None, endpoint_url: str | None = None
) -> Optional[str]:
    """Стоимость сессии («35.1 ₽», «10.5 $~») или None, если считать нечего / 0.

    Для списка /sessions: показываем даже крошечную ненулевую сумму (как
    «<0.01 $», а не молча прячем). Источник — персистентный ``meta.token_usage``.

    ``endpoint_url`` — текущий адрес бэкенда: нужен для сумм из старых сессий,
    записанных до того, как валюта стала фиксироваться на вызове.
    """
    account = UsageAccount.from_meta(meta)
    if account.is_empty():
        return None
    report: CostReport = compute_cost(account, pricing, model, endpoint_url)
    if not any(amount > 0 for amount in report.by_currency.values()):
        return None
    return _cost_display(report, compact=False)


def _segment_from_report(label: str, report: CostReport) -> tuple[str, str]:
    """(label, «<tok>» либо «<tok>/<cost>») по уже готовому отчёту."""
    tokens = format_tokens(report.total_input + report.total_output)
    cost = _cost_display(report, compact=True)
    return (label, f"{tokens}/{cost}" if cost else tokens)


def _segment(
    label: str,
    meta: Any,
    pricing: Any,
    model: str | None,
    endpoint_url: str | None = None,
) -> Optional[tuple[str, str]]:
    """(label, «<tok>» либо «<tok>/<cost>») или None, если в meta нет вызовов."""
    account = UsageAccount.from_meta(meta)
    if account.is_empty():
        return None
    report: CostReport = compute_cost(account, pricing, model, endpoint_url)
    return _segment_from_report(label, report)


def _to_text(seg: tuple[str, str] | None) -> Text | None:
    """(label, value) → Rich-Text «<label> <value>» или None."""
    if seg is None:
        return None
    lbl, value = seg
    t = Text()
    t.append(f"{lbl} ", style=_LABEL)
    t.append(value, style=_VALUE)
    return t


def _segment_text(
    label: str,
    meta: Any,
    pricing: Any,
    model: str | None,
    endpoint_url: str | None = None,
) -> Text | None:
    """Rich-Text одного сегмента «<label> <tok>[/<cost>]» или None."""
    return _to_text(_segment(label, meta, pricing, model, endpoint_url))


def _join(parts: list[Text], sep: str = _SEP) -> Text:
    out = Text()
    for i, p in enumerate(parts):
        if i:
            out.append(sep, style=_LABEL)
        out.append_text(p)
    return out


def usage_segments(
    session_meta: Any,
    request_meta: Any,
    pricing: Any,
    model: str | None,
    endpoint_url: str | None = None,
) -> tuple[Text | None, Text | None]:
    """Раздельные сегменты ``(request_text, session_text)`` для адаптивной вёрстки.

    Сессия пуста (провайдер не вернул токенов) → ``(None, None)`` (весь блок скрыт).
    Иначе session_text всегда есть; request_text может быть None (запроса ещё нет).

    Каждый сегмент показывает свои валюты: у запроса их обычно меньше, чем у
    сессии, и подгонять их друг под друга нельзя — это скрыло бы часть суммы."""
    session_account = UsageAccount.from_meta(session_meta)
    if session_account.is_empty():
        return (None, None)
    session_report: CostReport = compute_cost(
        session_account, pricing, model, endpoint_url
    )
    session = _to_text(_segment_from_report("сессия", session_report))
    request = _segment_text(
        "запрос", request_meta, pricing, model, endpoint_url
    )
    return (request, session)


def compose_adaptive_footer_right(
    head: Text,
    request_seg: Text | None,
    session_seg: Text | None,
    available_width: int,
    sep: str = _SEP,
) -> Text:
    """head (модель[· контекст]) + usage-сегменты в одну строку; если по ширине
    не влезает — токен-блок («запрос · сессия») уезжает на 2-ю строку целиком
    (футер height:2), 1-я строка остаётся без дыры между left и head.

    Стекуем только когда есть ОБА сегмента (иначе стекать нечего). ДальшеL1 не
    деградируем — по решению вёрстки."""
    usage = [s for s in (request_seg, session_seg) if s is not None]
    single = _join([head, *usage], sep)
    if (
        single.cell_len <= available_width
        or request_seg is None
        or session_seg is None
    ):
        return single
    out = Text()
    out.append_text(head)
    out.append("\n")
    out.append_text(_join(usage, sep))
    return out
