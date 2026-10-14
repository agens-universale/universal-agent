from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from gc_core.config_modules.field_meta import gc_meta

DEFAULT_CURRENCY = "₽"
DEFAULT_PER_TOKENS = 1_000_000


class ModelTariff(BaseModel):
    """Тариф одной модели. ``currency``/``per_tokens`` — опциональные override'ы
    глобальных дефолтов из :class:`PricingConfig` (None → берётся глобальный).

    ``cache_read`` — ставка за кэшированные input-токены (напр. DeepSeek 0.49 ₽/1M).
    Если не задана → кэшированные токены биллятся по обычной ``input``-ставке
    (обратная совместимость: провайдеры/модели без кэша не затронуты)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: float
    output: float
    cache_read: Optional[float] = None
    currency: Optional[str] = None
    per_tokens: Optional[int] = None


class PricingConfig(BaseModel):
    """Секция ``pricing`` из config.yml.

    Safe-by-default-OFF: пока ``enabled`` не True, стоимость не считается
    (токены при этом учитываются всегда, независимо от pricing).

    Верхнеуровневые ``input``/``output``/``cache_read`` — **дефолтный тариф**: применяется,
    когда фактическая модель вызова не найдена в ``models``. Для одно-модельного агента это удобнее,
    чем дублировать имя модели ключом. ``models`` остаётся для переопределения под
    конкретные модели (мультимодельные сетапы).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    currency: str = DEFAULT_CURRENCY
    per_tokens: int = DEFAULT_PER_TOKENS
    # Дефолтный тариф (fallback, когда модели нет в ``models``).
    input: Optional[float] = None
    output: Optional[float] = None
    cache_read: Optional[float] = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    models: dict[str, ModelTariff] = Field(
        default_factory=dict, json_schema_extra=gc_meta(expert=True)
    )


class ResolvedTariff(BaseModel):
    """Тариф конкретной модели после применения дефолт⊕override.

    ``cache_read`` — ставка кэшированных input-токенов; None → кэш биллится по ``input``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: float
    output: float
    cache_read: Optional[float] = None
    currency: str
    per_tokens: int


def _coerce_pricing(raw: Any) -> PricingConfig:
    if isinstance(raw, PricingConfig):
        return raw
    if isinstance(raw, dict):
        return PricingConfig.model_validate(raw)
    return PricingConfig()


# Публичный алиас для внешних модулей (см. usage/cost.py) — приватное имя
# остаётся ради вызовов внутри этого файла.
coerce_pricing = _coerce_pricing


def _rates_configured(input_rate: Any, output_rate: Any) -> bool:
    """Ставки заданы, если хотя бы одна из двух ненулевая.

    Оба нуля трактуются как «не задано»: показывать «0.0 ₽» — значит выдавать
    «цена неизвестна» за «бесплатно». ``cache_read: 0`` этим правилом не затронут —
    там ноль осмыслен.
    """
    if input_rate is None or output_rate is None:
        return False
    return float(input_rate) != 0 or float(output_rate) != 0


def pricing_has_rates(pricing: PricingConfig | dict | None) -> bool:
    """Задана ли хоть одна ставка: общий тариф или запись `models`. Без ставок
    стоимость остаётся скрытой (уведомление при старте), а не «неизвестной»."""
    pricing = _coerce_pricing(pricing)
    if not pricing.enabled:
        return False
    if _rates_configured(pricing.input, pricing.output):
        return True
    return any(_rates_configured(t.input, t.output) for t in pricing.models.values())


def resolve_model_tariff(
    pricing: PricingConfig | dict | None,
    model_name: str | None,
) -> Optional[ResolvedTariff]:
    """Тариф для модели.

    Порядок: сначала ищем ``model_name`` в ``models`` (точное переопределение);
    если не нашли — берём верхнеуровневый дефолтный тариф (``pricing.input/output``).

    Нулевые ставки на input и output трактуются как «не задано», а не как «бесплатно»:
    модель-специфичная запись (0, 0) откатывается к верхнеуровневому тарифу;
    верхнеуровневые (0, 0) возвращают None.

    Возвращает None, если pricing выключен или тарифа нет ни там, ни там — тогда
    вызывающий код показывает токены без цены.
    """
    pricing = _coerce_pricing(pricing)
    if not pricing.enabled:
        return None

    per_tokens = pricing.per_tokens
    currency: str = pricing.currency

    tariff = pricing.models.get(model_name) if model_name else None
    if tariff is not None and _rates_configured(tariff.input, tariff.output):
        # Переопределение под конкретную модель (с наследованием глобальных currency/per_tokens
        # и верхнеуровневого cache_read, если в модели он не задан).
        inp = float(tariff.input)
        out = float(tariff.output)
        cache = (
            tariff.cache_read
            if tariff.cache_read is not None
            else pricing.cache_read
        )
        if tariff.currency is not None:
            currency = tariff.currency
        if tariff.per_tokens is not None:
            per_tokens = tariff.per_tokens
    elif _rates_configured(pricing.input, pricing.output):
        # Дефолтный тариф верхнего уровня (одно-модельный сетап).
        inp = float(pricing.input)
        out = float(pricing.output)
        cache = pricing.cache_read
    else:
        return None

    if not isinstance(per_tokens, int) or per_tokens <= 0:
        per_tokens = DEFAULT_PER_TOKENS
    return ResolvedTariff(
        input=inp,
        output=out,
        cache_read=(float(cache) if cache is not None else None),
        currency=str(currency or DEFAULT_CURRENCY),
        per_tokens=per_tokens,
    )
