"""Стоимость на карточке сессии (/sessions) — единственный тест этого call-site.

Проверяем, что `_session_cost_label` берёт валюту факта из адреса эндпоинта
в конфиге приложения, а без факта считает по тарифу как раньше.

Экран не монтируем и приложение не запускаем: метод читает только `self.app`,
поэтому зовём его как несвязанную функцию с подставленным объектом. Продовый
код при этом исполняется целиком, включая чтение `completions_url` из конфига.
"""

from types import SimpleNamespace

import pytest
from gc_core.usage.account import CATEGORY_MAIN, UsageAccount

_PRICING = {
    "enabled": True,
    "input": 100,
    "output": 200,
    "currency": "₽",
    "per_tokens": 1_000_000,
}
_RU_URL = "https://routerai.ru/api/v1/chat/completions"
_USD_URL = "https://openrouter.ai/api/v1/chat/completions"


def _cost_label(config: dict, token_usage) -> str | None:
    """Вызов продового `_session_cost_label` с подставленным приложением."""
    from gc_testkit.settings import make_settings
    from gc_tui.screens.sessions import SessionsScreen

    loaded = make_settings(config)
    holder = SimpleNamespace(app=SimpleNamespace(base_settings=lambda: loaded))
    return SessionsScreen._session_cost_label(
        holder, SimpleNamespace(token_usage=token_usage)
    )


def _billed_meta(cost: float) -> dict:
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=cost, billed=True)
    return acc.to_meta()


def test_card_cost_uses_endpoint_currency():
    pytest.importorskip("textual")
    ru = {"model": "m", "pricing": _PRICING, "completions_url": _RU_URL}
    usd = {"model": "m", "pricing": _PRICING, "completions_url": _USD_URL}
    assert _cost_label(ru, _billed_meta(7.5)) == "7.50 ₽"
    assert _cost_label(usd, _billed_meta(7.5)) == "7.50 $"


def test_card_cost_without_fact_uses_tariff():
    pytest.importorskip("textual")
    acc = UsageAccount()
    acc.add(
        CATEGORY_MAIN, 1_000_000, 100_000
    )  # факта нет — считается по тарифу
    config = {"model": "m", "pricing": _PRICING, "completions_url": _RU_URL}
    assert _cost_label(config, acc.to_meta()) == "120.0 ₽"


def test_card_without_usage_gives_none():
    pytest.importorskip("textual")
    assert _cost_label({"model": "m", "pricing": _PRICING}, None) is None
