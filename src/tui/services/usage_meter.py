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

from rich.text import Text

from usage.account import UsageAccount
from usage.cost import CostReport, compute_cost
from usage.report import format_money, format_tokens

from tui.palette import DIM, INK

# Стиль меток/чисел подчинён модели в футере: метка приглушена, число — ink.
_LABEL = DIM
_VALUE = INK
# Разделитель сегментов футера (тот же, что у модель/контекст).
_SEP = "  ·  "


def _fact_currency(report: CostReport) -> Optional[str]:
    """Валюта фактической части отчёта или None, если факта нет.

    Берётся из самого отчёта, а не из константы: валюта факта зависит от
    провайдера (см. ``usage.currency``), поэтому «долларом» её считать нельзя.
    """
    for category in report.categories:
        for part in category.parts:
            if part.source == "provider":
                return part.currency
    return None


def _dominant_currency(report: CostReport) -> str:
    """Валюта с наибольшей долей токенов (суммы в разных валютах несравнимы,
    токены — всегда). При равенстве долей — валюта факта (детерминированность)."""
    tokens = report.tokens_by_currency
    if not tokens:
        return next(iter(report.by_currency))
    max_tokens = max(tokens.values())
    candidates = [c for c, t in tokens.items() if t == max_tokens]
    fact = _fact_currency(report)
    if fact is not None and fact in candidates:
        return fact
    return candidates[0]


def _headline_amount(report: CostReport, currency: str | None = None) -> Optional[tuple[float, str, str]]:
    """(сумма, валюта, маркеры) для компактного показа стоимости или None.

    ``currency`` — навязанная извне валюта (для парного показа «запрос»/«сессия»
    в одной валюте, см. ``usage_segments``); если её нет в отчёте — None (только
    токены у вызывающего, валюту НЕ переключаем). Без навязанной валюты — валюта
    блока выбирается по ``_dominant_currency`` (доля токенов, не сумм — суммы в
    разных валютах несравнимы). Маркеры: «~» — в отчёте есть ОБЕ стороны (и факт,
    и тариф) — часть суммы посчитана по тарифу, а не измерена (если сторона всего
    одна — тильда ничего не сообщает и не ставится); «+» — в показанную цифру
    вошло не всё (либо есть вызовы без цены вовсе, либо часть денег осталась в
    другой валюте — ``multi_currency`` — и в headline-сумму не попала).
    """
    if not report.has_cost:
        return None
    if currency is None:
        currency = _dominant_currency(report)
    elif currency not in report.by_currency:
        return None
    has_provider = any(p.source == "provider" for c in report.categories for p in c.parts)
    has_tariff = any(p.source == "tariff" for c in report.categories for p in c.parts)
    amount = report.by_currency[currency]
    marks = ""
    if has_provider and has_tariff:
        marks += "~"
    if report.has_unpriced or report.multi_currency:
        marks += "+"
    return (amount, currency, marks)


def format_nonzero_cost(meta: Any, pricing: Any, model: str | None,
                        endpoint_url: str | None = None) -> Optional[str]:
    """Стоимость сессии («35.1 ₽», «10.5 $~») или None, если считать нечего / 0.

    Для списка /sessions: показываем даже крошечную ненулевую сумму (как
    «<0.01 $», а не молча прячем). Источник — персистентный ``meta.token_usage``.

    ``endpoint_url`` — текущий адрес бэкенда. Эндпоинт в meta не хранится,
    поэтому валюта факта старой сессии считается по нынешнему конфигу — так же,
    как и модель для резолва тарифа.
    """
    account = UsageAccount.from_meta(meta)
    if account.is_empty():
        return None
    report: CostReport = compute_cost(account, pricing, model, endpoint_url)
    headline = _headline_amount(report)
    if headline is None:
        return None
    amount, currency, marks = headline
    if amount <= 0:
        return None
    return f"{format_money(amount, currency)}{marks}"


def _segment_from_report(label: str, report: CostReport, currency: str | None = None) -> tuple[str, str]:
    """(label, «<tok>» либо «<tok>/<cost>») по уже готовому отчёту."""
    tokens = format_tokens(report.total_input + report.total_output)
    headline = _headline_amount(report, currency)
    if headline is not None:
        amount, headline_currency, marks = headline
        # Компактно для футера: без пробела перед валютой («0.5₽», не «0.5 ₽»).
        value = f"{tokens}/{format_money(amount, headline_currency).replace(' ', '')}{marks}"
    else:
        value = tokens
    return (label, value)


def _segment(label: str, meta: Any, pricing: Any, model: str | None,
             currency: str | None = None, *,
             endpoint_url: str | None = None) -> Optional[tuple[str, str]]:
    """(label, «<tok>» либо «<tok>/<cost>») или None, если в meta нет вызовов.

    Две разные вещи, которые легко перепутать: ``currency`` — навязанная валюта
    ПОКАЗА (парный показ с другим сегментом, см. ``_headline_amount``);
    ``endpoint_url`` — источник валюты ФАКТА. Поэтому ``endpoint_url`` —
    keyword-only: позиционно он встал бы на место ``currency``.
    """
    account = UsageAccount.from_meta(meta)
    if account.is_empty():
        return None
    report: CostReport = compute_cost(account, pricing, model, endpoint_url)
    return _segment_from_report(label, report, currency)


def _to_text(seg: tuple[str, str] | None) -> Text | None:
    """(label, value) → Rich-Text «<label> <value>» или None."""
    if seg is None:
        return None
    lbl, value = seg
    t = Text()
    t.append(f"{lbl} ", style=_LABEL)
    t.append(value, style=_VALUE)
    return t


def _segment_text(label: str, meta: Any, pricing: Any, model: str | None,
                   currency: str | None = None, *,
                   endpoint_url: str | None = None) -> Text | None:
    """Rich-Text одного сегмента «<label> <tok>[/<cost>]» или None."""
    return _to_text(_segment(label, meta, pricing, model, currency,
                             endpoint_url=endpoint_url))


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

    Валюта решается один раз — по отчёту сессии (сессия — надмножество запроса) —
    и применяется к обоим сегментам: иначе «запрос» и «сессия» могли бы оказаться
    в разных валютах в одной строке без пояснения. Если в запросе денег в этой
    валюте нет — он остаётся с одними токенами, на другую валюту не переключается."""
    session_account = UsageAccount.from_meta(session_meta)
    if session_account.is_empty():
        return (None, None)
    session_report: CostReport = compute_cost(session_account, pricing, model, endpoint_url)
    session_headline = _headline_amount(session_report)
    currency = session_headline[1] if session_headline is not None else None
    session = _to_text(_segment_from_report("сессия", session_report, currency))
    request = _segment_text("запрос", request_meta, pricing, model, currency,
                            endpoint_url=endpoint_url)
    return (request, session)


def compose_adaptive_footer_right(
    head: Text,
    request_seg: Text | None,
    session_seg: Text | None,
    available_width: int,
    sep: str = _SEP,
) -> Text:
    """head (модель[· контекст]) + usage-сегменты в одну строку; если по ширине
    не влезает — «сессия» уезжает на 2-ю строку под «запрос» (футер height:2).

    Стекуем только когда есть ОБА сегмента (иначе стекать нечего). ДальшеL1 не
    деградируем — по решению вёрстки."""
    usage = [s for s in (request_seg, session_seg) if s is not None]
    single = _join([head, *usage], sep)
    if single.cell_len <= available_width or request_seg is None or session_seg is None:
        return single
    line1 = _join([head, request_seg], sep)
    out = Text()
    out.append_text(line1)
    out.append("\n")
    out.append_text(session_seg)
    return out


def render_usage_meter(
    session_meta: Any,
    last_run_meta: Any,
    pricing: Any,
    model: str | None,
    endpoint_url: str | None = None,
) -> Text | None:
    """Rich-Text «запрос …/… · сессия …/…» одной строкой или None (совместимость)."""
    request, session = usage_segments(session_meta, last_run_meta, pricing, model,
                                      endpoint_url)
    if session is None:
        return None
    return _join([s for s in (request, session) if s is not None])
