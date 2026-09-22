from gc_core.config_modules.pricing import (
    DEFAULT_CURRENCY,
    DEFAULT_PER_TOKENS,
    PricingConfig,
    pricing_from_config_dict,
    resolve_model_tariff,
)


def _pricing(**kw) -> dict:
    base = {
        "enabled": True,
        "currency": "₽",
        "per_tokens": 1_000_000,
        "models": {
            "MiniMaxAI/MiniMax-M2.7": {"input": 93, "output": 295},
        },
    }
    base.update(kw)
    return base


def test_resolve_inherits_global_defaults():
    t = resolve_model_tariff(_pricing(), "MiniMaxAI/MiniMax-M2.7")
    assert t is not None
    assert t.input == 93 and t.output == 295
    assert t.currency == "₽"
    assert t.per_tokens == 1_000_000


def test_resolve_per_model_override():
    pricing = _pricing(
        models={
            "MiniMaxAI/MiniMax-M2.7": {"input": 93, "output": 295},
            "some/usd-model": {
                "input": 0.05,
                "output": 0.15,
                "currency": "$",
                "per_tokens": 1000,
            },
        }
    )
    t = resolve_model_tariff(pricing, "some/usd-model")
    assert t is not None
    assert t.currency == "$"
    assert t.per_tokens == 1000
    assert t.input == 0.05 and t.output == 0.15


def test_resolve_disabled_returns_none():
    assert resolve_model_tariff(_pricing(enabled=False), "MiniMaxAI/MiniMax-M2.7") is None


def test_resolve_missing_model_returns_none():
    assert resolve_model_tariff(_pricing(), "unknown/model") is None
    assert resolve_model_tariff(_pricing(), None) is None


def test_resolve_accepts_pricing_config_instance():
    cfg = PricingConfig.model_validate(_pricing())
    t = resolve_model_tariff(cfg, "MiniMaxAI/MiniMax-M2.7")
    assert t is not None and t.input == 93


def test_invalid_per_tokens_falls_back_to_default():
    pricing = _pricing(
        per_tokens=0,
        models={"m": {"input": 1, "output": 2}},
    )
    t = resolve_model_tariff(pricing, "m")
    assert t is not None and t.per_tokens == DEFAULT_PER_TOKENS


def test_toplevel_default_tariff_used_when_model_absent():
    # Ставки на верхнем уровне → применяются к любой модели без записи в models.
    pricing = {
        "enabled": True,
        "currency": "₽",
        "per_tokens": 1_000_000,
        "input": 59,
        "output": 118,
        "cache_read": 0.49,
    }
    t = resolve_model_tariff(pricing, "deepseek/deepseek-v4-pro")
    assert t is not None
    assert t.input == 59 and t.output == 118 and t.cache_read == 0.49
    # имя модели в ключах не нужно
    t2 = resolve_model_tariff(pricing, "any/other-model")
    assert t2 is not None and t2.input == 59


def test_model_override_wins_over_toplevel():
    pricing = {
        "enabled": True,
        "currency": "₽",
        "per_tokens": 1_000_000,
        "input": 59,
        "output": 118,
        "cache_read": 0.49,
        "models": {"special/model": {"input": 10, "output": 20}},
    }
    # модель из models → её ставки
    t = resolve_model_tariff(pricing, "special/model")
    assert t.input == 10 and t.output == 20
    # модель без записи → верхний дефолт
    t2 = resolve_model_tariff(pricing, "deepseek/deepseek-v4-pro")
    assert t2.input == 59


def test_model_inherits_toplevel_cache_read():
    # Модель без cache_read наследует верхнеуровневый cache_read.
    pricing = {
        "enabled": True,
        "currency": "₽",
        "per_tokens": 1_000_000,
        "cache_read": 0.49,
        "models": {"m": {"input": 59, "output": 118}},
    }
    t = resolve_model_tariff(pricing, "m")
    assert t.cache_read == 0.49


def test_no_toplevel_no_model_returns_none():
    pricing = {"enabled": True, "currency": "₽", "per_tokens": 1_000_000}
    assert resolve_model_tariff(pricing, "anything") is None


def test_pricing_from_config_dict_defaults():
    assert pricing_from_config_dict(None).enabled is False
    assert pricing_from_config_dict({}).currency == DEFAULT_CURRENCY
    parsed = pricing_from_config_dict({"pricing": _pricing()})
    assert parsed.enabled is True
    assert "MiniMaxAI/MiniMax-M2.7" in parsed.models


def test_zero_rates_mean_not_configured():
    # config.default.yml идёт с нулями — это «ставки не заданы», а не «бесплатно».
    pricing = {"enabled": True, "currency": "₽", "per_tokens": 1_000_000, "input": 0, "output": 0}
    assert resolve_model_tariff(pricing, "any/model") is None


def test_single_zero_rate_is_valid():
    # Ноль в одной из двух ставок осмыслен (например, бесплатный вход).
    pricing = {"enabled": True, "currency": "₽", "per_tokens": 1_000_000, "input": 0, "output": 5}
    t = resolve_model_tariff(pricing, "any/model")
    assert t is not None
    assert t.input == 0
    assert t.output == 5


def test_zero_model_entry_falls_back_to_toplevel():
    pricing = {
        "enabled": True,
        "currency": "₽",
        "per_tokens": 1_000_000,
        "input": 59,
        "output": 118,
        "models": {"m": {"input": 0, "output": 0}},
    }
    t = resolve_model_tariff(pricing, "m")
    assert t is not None
    assert (t.input, t.output) == (59, 118)


def test_zero_cache_read_is_a_valid_rate():
    pricing = {"enabled": True, "currency": "₽", "per_tokens": 1_000_000, "input": 59, "output": 118, "cache_read": 0}
    t = resolve_model_tariff(pricing, "any/model")
    assert t.cache_read == 0
