"""Нормализация ``usage`` из ответов бэкендов к ``UsageSample``.

Две формы токенов:
- chat_completions: ``{"prompt_tokens": .., "completion_tokens": ..}``;
- responses:        ``{"input_tokens": .., "output_tokens": ..}``.

Кэшированные input-токены (биллятся по cache-ставке) ищутся в:
- OpenAI/RouterAI: ``prompt_tokens_details.cached_tokens``;
- DeepSeek-native: ``prompt_cache_hit_tokens``.
``cached`` — это часть ``input`` (cached ≤ input), не отдельные токены.

Возвращает ``UsageSample`` или ``None``, если usage отсутствует/битый — вызывающий код тогда
фиксирует пропуск (``UsageAccount.add_missing``) и НЕ добавляет токены.
Функция никогда не кидает исключение.
"""

from __future__ import annotations

import math
from typing import Any, NamedTuple, Optional, Tuple


class UsageSample(NamedTuple):
    """Разобранный ``usage`` одного вызова.

    ``cost`` — фактическая стоимость от провайдера (OpenRouter кладёт её в каждый
    ответ). Валюту поле не сообщает — она выводится из адреса эндпоинта, см.
    ``usage/currency.py``. ``billed`` — «цена этого вызова известна»: отделяет
    бесплатную модель (``cost: 0``) от отсутствия цены.
    """

    input: int
    output: int
    cached: int
    cost: Optional[float] = None
    billed: bool = False


# (chat_completions ключ, responses ключ)
_INPUT_KEYS = ("prompt_tokens", "input_tokens")
_OUTPUT_KEYS = ("completion_tokens", "output_tokens")
# Плоские ключи кэша (DeepSeek-native и пр.)
_CACHED_FLAT_KEYS = (
    "prompt_cache_hit_tokens",
    "cached_tokens",
    "cache_read_input_tokens",
)
# Вложенные контейнеры с ``cached_tokens`` внутри
_CACHED_DETAIL_KEYS = ("prompt_tokens_details", "input_tokens_details")


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    return None


def _pick(raw: dict, keys: Tuple[str, ...]) -> Optional[int]:
    for key in keys:
        if key in raw:
            coerced = _coerce_int(raw.get(key))
            if coerced is not None:
                return coerced
    return None


def _pick_cached(raw: dict) -> int:
    """Кэшированные input-токены. 0, если поля нет."""
    flat = _pick(raw, _CACHED_FLAT_KEYS)
    if flat is not None:
        return flat
    for key in _CACHED_DETAIL_KEYS:
        details = raw.get(key)
        if isinstance(details, dict):
            nested = _coerce_int(details.get("cached_tokens"))
            if nested is not None:
                return nested
    return 0


def _is_byok(raw: dict) -> bool:
    """``is_byok`` — bool или строка ("true"/"false"), провайдер-агностично."""
    value = raw.get("is_byok")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _pick_cost(raw: dict) -> Optional[float]:
    """Фактическая стоимость вызова или None, если её нет/ей нельзя доверять."""
    # BYOK: неизвестно, лежит в cost комиссия или полная цена (см. спеку §4.4).
    if _is_byok(raw):
        return None
    value = raw.get("cost")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or not math.isfinite(value):
        return None
    return float(value)


def normalize_usage(raw: Any) -> Optional[UsageSample]:
    """Вернуть ``UsageSample`` с токенами и стоимостью или None, если usage не пришёл.

    ``cached`` — часть ``input`` (доля, попавшая в кэш-ставку); ограничена сверху input.
    Режим бэкенда не нужен: оба набора ключей пробуются одновременно, что
    устойчиво к расхождениям прокси/провайдеров.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    input_tokens = _pick(raw, _INPUT_KEYS)
    output_tokens = _pick(raw, _OUTPUT_KEYS)
    if input_tokens is None and output_tokens is None:
        return None
    inp = input_tokens or 0
    cached = min(_pick_cached(raw), inp)  # cached не может превышать input
    cost = _pick_cost(raw)
    return UsageSample(
        input=inp,
        output=output_tokens or 0,
        cached=cached,
        cost=cost,
        billed=cost is not None,
    )
