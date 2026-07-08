"""render_usage_meter: футер «запрос <tok>/<cost> · сессия <tok>/<cost>».

Правила видимости:
- сессия пуста (нет токенов провайдера) → None (весь блок скрыт);
- тариф выключен → только токены, без цены;
- итога запроса ещё нет → только сегмент сессии.
"""
from usage.account import CATEGORY_MAIN, UsageAccount

from tui.services.usage_meter import format_nonzero_cost, render_usage_meter

_PRICING_ON = {
    "enabled": True,
    "input": 1.0,
    "output": 2.0,
    "currency": "₽",
    "per_tokens": 1_000_000,
}
_PRICING_OFF = {"enabled": False}
_MODEL = "deepseek/deepseek-v4"


def _meta(input_tokens: int, output_tokens: int) -> dict:
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, input_tokens, output_tokens)
    return acc.to_meta()


def test_hidden_when_session_empty():
    assert render_usage_meter(UsageAccount().to_meta(), None, _PRICING_ON, _MODEL) is None


def test_hidden_when_session_meta_none():
    assert render_usage_meter(None, None, _PRICING_ON, _MODEL) is None


def test_tokens_and_cost_when_pricing_on():
    session = _meta(2_000_000, 0)      # total 2.0M, cost 2.0 ₽
    last_run = _meta(1_000_000, 0)     # total 1.0M, cost 1.0 ₽
    m = render_usage_meter(session, last_run, _PRICING_ON, _MODEL)
    assert m is not None
    assert m.plain == "запрос 1.0M/1.0₽  ·  сессия 2.0M/2.0₽"


def test_tokens_only_when_pricing_off():
    session = _meta(2_000_000, 0)
    last_run = _meta(1_000_000, 0)
    m = render_usage_meter(session, last_run, _PRICING_OFF, _MODEL)
    assert m is not None
    assert m.plain == "запрос 1.0M  ·  сессия 2.0M"


def test_session_only_when_no_request_total():
    session = _meta(2_000_000, 0)
    m = render_usage_meter(session, None, _PRICING_ON, _MODEL)
    assert m is not None
    assert m.plain == "сессия 2.0M/2.0₽"


def test_session_only_when_request_meta_empty():
    session = _meta(2_000_000, 0)
    m = render_usage_meter(session, UsageAccount().to_meta(), _PRICING_ON, _MODEL)
    assert m is not None
    assert m.plain == "сессия 2.0M/2.0₽"


def test_format_nonzero_cost_shows_money():
    assert format_nonzero_cost(_meta(2_000_000, 0), _PRICING_ON, _MODEL) == "2.0 ₽"


def test_format_nonzero_cost_hidden_when_zero_or_no_pricing():
    assert format_nonzero_cost(UsageAccount().to_meta(), _PRICING_ON, _MODEL) is None
    assert format_nonzero_cost(_meta(2_000_000, 0), _PRICING_OFF, _MODEL) is None
    # крошечный usage → после half-up до 0.1 всё ещё 0.0
    assert format_nonzero_cost(_meta(1, 0), _PRICING_ON, _MODEL) is None
