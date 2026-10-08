from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from gc_core.config_modules.summary import (
    DEFAULT_INCREMENTAL_BATCH_MULTIPLIER,
    DEFAULT_TURN_END_TRIGGER_RATIO,
    SUMMARY_CHARS_PER_TOKEN,
)


@dataclass(frozen=True)
class ResolvedSummaryLimits:
    """Эффективные пороги сжатия для алиаса истории на момент проверки."""

    chars_during: int
    tokens_during: int
    chars_after: int
    tokens_after: int
    incremental_batch_chars: int
    max_dialogue_chars: int


def parse_summary_trigger_chars(config_dict: Dict[str, Any]) -> int:
    """
    Порог символов истории для запуска compaction в режиме chat_completions.

    Берётся из config_dict['summary_params']['chat_completions_summary_trigger_chars'].
    При ошибке приведения или отсутствии секции возвращает 0 (compaction отключён).
    """
    return _parse_summary_int(
        config_dict, "chat_completions_summary_trigger_chars"
    )


def parse_summary_trigger_tokens(config_dict: Dict[str, Any]) -> int:
    """Порог input-токенов для mid-turn / pre-run. 0 — плечо выключено."""
    return _parse_summary_int(
        config_dict, "chat_completions_summary_trigger_tokens"
    )


def parse_summary_turn_end_trigger_chars(config_dict: Dict[str, Any]) -> int:
    """
    Порог turn-end compaction после закрытия turn (``turn_finish``).

    Ожидается уже нормализованное значение в ``summary_params`` после ``load_config``
    / ``resolve_paths``. Парсер только читает int; дефолт 60% mid-turn задаётся в конфиг-модуле.
    """
    return _parse_turn_end_int(
        config_dict, "chat_completions_summary_turn_end_trigger_chars"
    )


def parse_summary_turn_end_trigger_tokens(config_dict: Dict[str, Any]) -> int:
    """Порог input-токенов после accept ``turn_finish``. 0 — плечо выключено.

    60% от токенного mid-turn считает ``load_config``, если ключа не было.
    """
    return _parse_turn_end_int(
        config_dict, "chat_completions_summary_turn_end_trigger_tokens"
    )


def summary_mid_turn_enabled(config_dict: Dict[str, Any]) -> bool:
    """Mid-turn (pre-run и pre-backend) включён, если хотя бы один его порог > 0."""
    return (
        parse_summary_trigger_chars(config_dict) > 0
        or parse_summary_trigger_tokens(config_dict) > 0
    )


def resolve_summary_limits(
    config_dict: Dict[str, Any],
    *,
    model_chars: int = 0,
    model_tokens: int = 0,
) -> ResolvedSummaryLimits:
    """Пороги mid-turn / turn-end / батч / диалог для алиаса истории.

    Оба порога модели ``<= 0`` — весь набор из глобального ``summary_params``.
    Хотя бы один ``> 0`` — режим модели: незаданная шкала mid и turn-end = 0;
    батч и диалог считаются коэффициентами от символьного основания.
    """
    chars = model_chars if model_chars > 0 else 0
    tokens = model_tokens if model_tokens > 0 else 0
    if chars > 0 or tokens > 0:
        chars_after = (
            int(chars * DEFAULT_TURN_END_TRIGGER_RATIO) if chars > 0 else 0
        )
        tokens_after = (
            int(tokens * DEFAULT_TURN_END_TRIGGER_RATIO) if tokens > 0 else 0
        )
        char_base = chars if chars > 0 else tokens * SUMMARY_CHARS_PER_TOKEN
        return ResolvedSummaryLimits(
            chars_during=chars,
            tokens_during=tokens,
            chars_after=chars_after,
            tokens_after=tokens_after,
            incremental_batch_chars=int(
                char_base * DEFAULT_INCREMENTAL_BATCH_MULTIPLIER
            ),
            max_dialogue_chars=int(char_base * 5),
        )
    return ResolvedSummaryLimits(
        chars_during=parse_summary_trigger_chars(config_dict),
        tokens_during=parse_summary_trigger_tokens(config_dict),
        chars_after=parse_summary_turn_end_trigger_chars(config_dict),
        tokens_after=parse_summary_turn_end_trigger_tokens(config_dict),
        incremental_batch_chars=_parse_summary_int(
            config_dict, "incremental_batch_chars"
        ),
        max_dialogue_chars=_parse_summary_int(
            config_dict, "max_dialogue_chars"
        ),
    )


def resolve_summary_limits_for_resolved(
    config_dict: Dict[str, Any],
    resolved: Any | None,
) -> ResolvedSummaryLimits:
    """Резолв по ``ResolvedModel`` (легаси / None → оба порога 0 → глобаль)."""
    return resolve_summary_limits(
        config_dict,
        model_chars=int(getattr(resolved, "summary_trigger_chars", 0) or 0),
        model_tokens=int(getattr(resolved, "summary_trigger_tokens", 0) or 0),
    )


def resolve_summary_limits_at_level(
    config_dict: Dict[str, Any],
    router: Any | None,
    level: str | None,
) -> ResolvedSummaryLimits:
    """Пороги модели, которая ведёт историю, — той же, в которую идут запросы.

    ``level`` — уровень вызова (сегодня — зафиксированный на ход
    ``state.turn_level``): живой ``/level`` посреди хода модель запросов не
    меняет, и порог обязан остаться её порогом. Нет router / ошибка → глобаль.
    Предохранитель не проверяется: порог — свойство записи модели."""
    resolved = None
    if router is not None:
        try:
            resolved = router.resolve_main_at_level(level, check_breaker=False)
        except Exception:
            resolved = None
    return resolve_summary_limits_for_resolved(config_dict, resolved)


def _summary_params(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    raw = config_dict.get("summary_params")
    return raw if isinstance(raw, dict) else {}


def _parse_summary_int(config_dict: Dict[str, Any], key: str) -> int:
    try:
        return int(_summary_params(config_dict).get(key, 0))
    except (TypeError, ValueError):
        return 0


def _parse_turn_end_int(config_dict: Dict[str, Any], key: str) -> int:
    value = _parse_summary_int(config_dict, key)
    return value if value > 0 else 0
