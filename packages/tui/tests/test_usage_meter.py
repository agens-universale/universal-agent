"""Футер usage-метра (`usage_segments` одной строкой): «запрос <tok>/<cost> · сессия <tok>/<cost>».

Правила видимости:
- сессия пуста (нет токенов провайдера) → None (весь блок скрыт);
- тариф выключен → только токены, без цены;
- итога запроса ещё нет → только сегмент сессии.
"""

from gc_core.usage.account import (
    CATEGORY_FINISH_GATE,
    CATEGORY_MAIN,
    UsageAccount,
)
from gc_core.usage.cost import compute_cost
from gc_core.usage.report import render_cost_report
from gc_tui.services.usage_meter import (
    _join,
    format_nonzero_cost,
    usage_segments,
)


def render_usage_meter(
    session_meta, last_run_meta, pricing, model, endpoint_url=None
):
    """«запрос …/… · сессия …/…» одной строкой или None — оба сегмента футера
    `usage_segments`, склеенные разделителем футера."""
    request, session = usage_segments(
        session_meta, last_run_meta, pricing, model, endpoint_url
    )
    if session is None:
        return None
    return _join([s for s in (request, session) if s is not None])


_PRICING_ON = {
    "enabled": True,
    "input": 1.0,
    "output": 2.0,
    "currency": "₽",
    "per_tokens": 1_000_000,
}
_PRICING_OFF = {"enabled": False}
_PRICING_RUB = {
    "enabled": True,
    "input": 100,
    "output": 200,
    "currency": "₽",
    "per_tokens": 1_000_000,
}
_PRICING_NO_RATES = {"enabled": True, "currency": "₽", "per_tokens": 1_000_000}
_MODEL = "deepseek/deepseek-v4"


def _meta(input_tokens: int, output_tokens: int) -> dict:
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, input_tokens, output_tokens)
    return acc.to_meta()


def test_hidden_when_session_empty():
    assert (
        render_usage_meter(UsageAccount().to_meta(), None, _PRICING_ON, _MODEL)
        is None
    )


def test_hidden_when_session_meta_none():
    assert render_usage_meter(None, None, _PRICING_ON, _MODEL) is None


def test_tokens_and_cost_when_pricing_on():
    # Чисто тарифный отчёт (нет факта провайдера) — тильды нет нигде.
    session = _meta(2_000_000, 0)  # total 2.0M, cost 2.0 ₽
    last_run = _meta(1_000_000, 0)  # total 1.0M, cost 1.0 ₽
    m = render_usage_meter(session, last_run, _PRICING_ON, _MODEL)
    assert m is not None
    assert m.plain == "запрос 1.0M/1.00₽ · сессия 2.0M/2.00₽"


def test_tokens_only_when_pricing_off():
    session = _meta(2_000_000, 0)
    last_run = _meta(1_000_000, 0)
    m = render_usage_meter(session, last_run, _PRICING_OFF, _MODEL)
    assert m is not None
    assert m.plain == "запрос 1.0M · сессия 2.0M"


def test_session_only_when_no_request_total():
    session = _meta(2_000_000, 0)
    m = render_usage_meter(session, None, _PRICING_ON, _MODEL)
    assert m is not None
    assert m.plain == "сессия 2.0M/2.00₽"


def test_session_only_when_request_meta_empty():
    session = _meta(2_000_000, 0)
    m = render_usage_meter(
        session, UsageAccount().to_meta(), _PRICING_ON, _MODEL
    )
    assert m is not None
    assert m.plain == "сессия 2.0M/2.00₽"


def test_format_nonzero_cost_shows_money():
    assert (
        format_nonzero_cost(_meta(2_000_000, 0), _PRICING_ON, _MODEL)
        == "2.00 ₽"
    )


def test_format_nonzero_cost_hidden_when_zero_or_no_pricing():
    assert (
        format_nonzero_cost(UsageAccount().to_meta(), _PRICING_ON, _MODEL)
        is None
    )
    assert (
        format_nonzero_cost(_meta(2_000_000, 0), _PRICING_OFF, _MODEL) is None
    )
    # крошечный usage по тарифу → не прячем, показываем нижний порог (без «~» — факта нет)
    assert format_nonzero_cost(_meta(1, 0), _PRICING_ON, _MODEL) == "<0.01 ₽"


def test_footer_shows_provider_currency_and_tilde_on_mixed():
    acc = UsageAccount()
    acc.add(
        CATEGORY_MAIN,
        1_000_000,
        100_000,
        0,
        cost=10.45,
        billed=True,
        currency="$",
    )
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    text = render_usage_meter(acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m")
    assert "10.5$+60.0₽~" in text.plain


def test_footer_shows_every_currency_never_hides_money():
    # Обе валюты видны целиком: доля токенов не пропорциональна деньгам, поэтому
    # свернуть картину до одной цифры значило бы спрятать неизвестно какую сумму.
    acc = UsageAccount()
    acc.add(
        CATEGORY_MAIN,
        1_000_000,
        100_000,
        0,
        cost=10.45,
        billed=True,
        currency="$",
    )
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    text = render_usage_meter(
        acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m"
    ).plain
    assert "10.5$" in text and "60.0₽" in text
    assert "?" not in text  # неполноты нет: у каждой траты есть цена


def test_footer_plain_when_all_billed():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=10.45, billed=True)
    text = render_usage_meter(acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m")
    assert "10.5$" in text.plain
    assert "~" not in text.plain


def test_footer_marks_unpriced_with_question():
    # «?» — есть трата, цену которой узнать нечем (ни факта, ни тарифа).
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.5, billed=True, currency="$")
    acc.add(CATEGORY_MAIN, 1000, 100)
    text = render_usage_meter(
        acc.to_meta(), acc.to_meta(), _PRICING_NO_RATES, "m"
    )
    assert "0.50$?" in text.plain


def test_footer_marks_two_currencies_and_unpriced_together():
    # Худший случай: провайдера меняли посреди треда (две валюты факта), а тарифа
    # нет — значит часть трат осталась без цены вовсе. Знаки «+» и «?» вместе.
    # Тильды здесь быть не может: тариф резолвится на весь отчёт, поэтому «часть
    # посчитана по тарифу» и «тарифа нет» — взаимоисключающие состояния.
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
    acc.add(
        CATEGORY_MAIN,
        500_000,
        50_000,
        0,
        cost=200.0,
        billed=True,
        currency="₽",
    )
    acc.add(CATEGORY_MAIN, 1000, 100)  # цены нет вовсе
    text = render_usage_meter(
        acc.to_meta(), acc.to_meta(), _PRICING_NO_RATES, "m"
    ).plain
    assert "2.00$+200.0₽?" in text
    assert "~" not in text


def test_sessions_shows_tiny_cost_instead_of_hiding():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.002, billed=True, currency="$")
    assert format_nonzero_cost(acc.to_meta(), _PRICING_RUB, "m") == "<0.01 $"


def test_sessions_shows_all_currencies():
    acc = UsageAccount()
    acc.add(
        CATEGORY_MAIN,
        1_000_000,
        100_000,
        0,
        cost=10.45,
        billed=True,
        currency="$",
    )
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    # Карточка сессии шире футера — обе валюты помещаются полностью.
    assert (
        format_nonzero_cost(acc.to_meta(), _PRICING_RUB, "m")
        == "10.5 $+60.0 ₽~"
    )


def test_footer_segments_keep_their_own_currencies():
    # В сессии есть факт в долларах, в текущем ходе — только тарифная часть в рублях.
    # Каждый сегмент показывает свои деньги: подгонять их друг под друга нельзя —
    # это скрыло бы часть суммы.
    session = UsageAccount()
    session.add(
        CATEGORY_MAIN,
        1_000_000,
        100_000,
        0,
        cost=10.45,
        billed=True,
        currency="$",
    )
    session.add(CATEGORY_MAIN, 500_000, 50_000)
    request = UsageAccount()
    request.add(CATEGORY_MAIN, 500_000, 50_000)
    text = render_usage_meter(
        session.to_meta(), request.to_meta(), _PRICING_RUB, "m"
    ).plain
    assert "запрос 550.0k/60.0₽" in text
    assert "сессия 1.6M/10.5$+60.0₽~" in text


def test_same_currency_mix_marks_tilde_only():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=10.45, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    pricing_usd = {
        "enabled": True,
        "currency": "$",
        "per_tokens": 1_000_000,
        "input": 100,
        "output": 200,
    }
    text = render_usage_meter(
        acc.to_meta(), acc.to_meta(), pricing_usd, "m"
    ).plain
    assert "~" in text
    assert "+" not in text


def test_footer_currency_order_follows_token_share():
    # Порядок валют детерминирован: первой идёт валюта категории с наибольшими
    # токенами. Иначе футер прыгал бы между перерисовками.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 5_000_000, 200_000)  # тариф ₽
    acc.add(
        CATEGORY_FINISH_GATE,
        3_000,
        100,
        0,
        cost=0.004,
        billed=True,
        currency="$",
    )
    text = render_usage_meter(
        acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m"
    ).plain
    assert "540.0₽+<0.01$~" in text


def test_tilde_only_when_both_sides_present():
    # Чисто тарифный отчёт: тильды нет.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000)
    assert (
        "~"
        not in render_usage_meter(
            acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m"
        ).plain
    )
    text = render_cost_report(
        compute_cost(acc, _PRICING_RUB, "m"), model_name="m"
    )
    assert "~" not in text


def test_tilde_present_on_real_mix():
    # Реальное смешение (факт + тариф в сопоставимых долях) — правило Б не должно
    # выродиться в «тильды не бывает никогда».
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 100_000, 0, cost=10.45, billed=True)
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    text = render_usage_meter(
        acc.to_meta(), acc.to_meta(), _PRICING_RUB, "m"
    ).plain
    assert "~" in text


# ---------------- валюта факта по эндпоинту ----------------

_RU_URL = "https://routerai.ru/api/v1/chat/completions"


def test_footer_shows_rubles_on_ru_endpoint():
    # Факт от .ru-провайдера + рублёвый тариф: одна валюта, тильда есть,
    # соединителя «+» нет — складывать нечего.
    acc = UsageAccount()
    acc.add(
        CATEGORY_MAIN,
        1_000_000,
        100_000,
        0,
        cost=10.45,
        billed=True,
        currency="₽",
    )
    acc.add(CATEGORY_MAIN, 500_000, 50_000)
    text = render_usage_meter(
        acc.to_meta(), acc.to_meta(), _PRICING_RUB, _MODEL, _RU_URL
    ).plain
    assert "70.5₽~" in text
    assert "$" not in text
    assert "+" not in text


def test_sessions_label_uses_ru_currency():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=7.5, billed=True, currency="₽")
    assert format_nonzero_cost(acc.to_meta(), _PRICING_RUB, _MODEL) == "7.50 ₽"


def test_legacy_meta_currency_resolved_by_endpoint():
    # Суммы старых сессий валюты не несут — она берётся из текущего конфига,
    # ровно как вела себя предыдущая версия.
    acc = UsageAccount()
    acc.add(
        CATEGORY_MAIN, 1_000_000, 0, 0, cost=1.0, billed=True
    )  # без валюты
    ru = render_usage_meter(
        acc.to_meta(), acc.to_meta(), _PRICING_RUB, _MODEL, _RU_URL
    ).plain
    assert "1.00₽" in ru and "$" not in ru
    usd = render_usage_meter(
        acc.to_meta(), acc.to_meta(), _PRICING_RUB, _MODEL
    ).plain
    assert "1.00$" in usd


def test_legacy_and_fresh_sums_merge_into_one_part():
    # Старая сумма (без валюты) и новая в той же валюте — одно слагаемое, не два.
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1_000_000, 0, 0, cost=1.0, billed=True)  # legacy
    acc.add(
        CATEGORY_MAIN, 1_000_000, 0, 0, cost=2.0, billed=True, currency="₽"
    )
    text = render_usage_meter(
        acc.to_meta(), acc.to_meta(), _PRICING_RUB, _MODEL, _RU_URL
    ).plain
    assert "3.00₽" in text
    assert "+" not in text
