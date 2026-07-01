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


def _segment(label: str, meta: Any, pricing: Any, model: str | None) -> Optional[tuple[str, str]]:
    """(label, «<tok>» либо «<tok>/<cost>») или None, если в meta нет вызовов."""
    account = UsageAccount.from_meta(meta)
    if account.is_empty():
        return None
    report: CostReport = compute_cost(account, pricing, model)
    tokens = format_tokens(report.total_input + report.total_output)
    # Цена — только при найденном тарифе и единственной валюте (одно-модельный агент).
    if report.has_pricing and not report.multi_currency and report.by_currency:
        currency, amount = next(iter(report.by_currency.items()))
        # Компактно для футера: без пробела перед валютой («0.5₽», не «0.5 ₽»).
        value = f"{tokens}/{format_money(amount, currency).replace(' ', '')}"
    else:
        value = tokens
    return (label, value)


def _segment_text(label: str, meta: Any, pricing: Any, model: str | None) -> Text | None:
    """Rich-Text одного сегмента «<label> <tok>[/<cost>]» или None."""
    seg = _segment(label, meta, pricing, model)
    if seg is None:
        return None
    lbl, value = seg
    t = Text()
    t.append(f"{lbl} ", style=_LABEL)
    t.append(value, style=_VALUE)
    return t


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
) -> tuple[Text | None, Text | None]:
    """Раздельные сегменты ``(request_text, session_text)`` для адаптивной вёрстки.

    Сессия пуста (провайдер не вернул токенов) → ``(None, None)`` (весь блок скрыт).
    Иначе session_text всегда есть; request_text может быть None (запроса ещё нет)."""
    session = _segment_text("сессия", session_meta, pricing, model)
    if session is None:
        return (None, None)
    request = _segment_text("запрос", request_meta, pricing, model)
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
) -> Text | None:
    """Rich-Text «запрос …/… · сессия …/…» одной строкой или None (совместимость)."""
    request, session = usage_segments(session_meta, last_run_meta, pricing, model)
    if session is None:
        return None
    return _join([s for s in (request, session) if s is not None])
