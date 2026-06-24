"""Нормализация ``usage`` из ответов бэкендов к тройке (input, output, cached).

Две формы токенов:
- chat_completions: ``{"prompt_tokens": .., "completion_tokens": ..}``;
- responses:        ``{"input_tokens": .., "output_tokens": ..}``.

Кэшированные input-токены (биллятся по cache-ставке) ищутся в:
- OpenAI/RouterAI: ``prompt_tokens_details.cached_tokens``;
- DeepSeek-native: ``prompt_cache_hit_tokens``.
``cached`` — это часть ``input`` (cached ≤ input), не отдельные токены.

Возвращает ``None``, если usage отсутствует/битый — вызывающий код тогда
фиксирует пропуск (``UsageAccount.add_missing``) и НЕ добавляет токены.
Функция никогда не кидает исключение.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

# (chat_completions ключ, responses ключ)
_INPUT_KEYS = ("prompt_tokens", "input_tokens")
_OUTPUT_KEYS = ("completion_tokens", "output_tokens")
# Плоские ключи кэша (DeepSeek-native и пр.)
_CACHED_FLAT_KEYS = ("prompt_cache_hit_tokens", "cached_tokens", "cache_read_input_tokens")
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


def normalize_usage(raw: Any, backend_mode: str | None = None) -> Optional[Tuple[int, int, int]]:
    """Вернуть (input_tokens, output_tokens, cached_tokens) или None, если usage не пришёл.

    ``cached`` — часть ``input`` (доля, попавшая в кэш-ставку); ограничена сверху input.
    ``backend_mode`` сейчас не обязателен: оба набора ключей пробуются
    одновременно, что устойчиво к расхождениям прокси/провайдеров.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    input_tokens = _pick(raw, _INPUT_KEYS)
    output_tokens = _pick(raw, _OUTPUT_KEYS)
    if input_tokens is None and output_tokens is None:
        return None
    inp = input_tokens or 0
    cached = min(_pick_cached(raw), inp)  # cached не может превышать input
    return (inp, output_tokens or 0, cached)
