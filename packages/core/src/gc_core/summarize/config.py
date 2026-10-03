from typing import Any

from gc_core.settings import settings_of


def parse_summary_trigger_chars(settings: Any) -> int:
    """
    Порог символов истории для запуска compaction в режиме chat_completions.

    Берётся из ``settings.summary.chat_completions_summary_trigger_chars``.
    При ошибке приведения возвращает 0 (compaction отключён).
    """
    return _parse_summary_int(
        settings, "chat_completions_summary_trigger_chars"
    )


def parse_summary_trigger_tokens(settings: Any) -> int:
    """Порог input-токенов для mid-turn / pre-run. 0 — плечо выключено."""
    return _parse_summary_int(
        settings, "chat_completions_summary_trigger_tokens"
    )


def parse_summary_turn_end_trigger_chars(settings: Any) -> int:
    """
    Порог turn-end compaction после закрытия turn (``turn_finish``).

    Ожидается уже нормализованное значение в ``settings.summary`` после ``load_settings``
    / ``resolve_paths``. Парсер только читает int; дефолт 60% mid-turn задаётся в конфиг-модуле.
    """
    return _parse_turn_end_int(
        settings, "chat_completions_summary_turn_end_trigger_chars"
    )


def parse_summary_turn_end_trigger_tokens(settings: Any) -> int:
    """Порог input-токенов после accept ``turn_finish``. 0 — плечо выключено.

    60% от токенного mid-turn считает загрузка настроек (``resolve_paths``), если ключа не было.
    """
    return _parse_turn_end_int(
        settings, "chat_completions_summary_turn_end_trigger_tokens"
    )


def summary_mid_turn_enabled(settings: Any) -> bool:
    """Mid-turn (pre-run и pre-backend) включён, если хотя бы один его порог > 0."""
    return (
        parse_summary_trigger_chars(settings) > 0
        or parse_summary_trigger_tokens(settings) > 0
    )


def _parse_summary_int(settings: Any, key: str) -> int:
    """``settings`` — всё, что принимает ``settings_of`` (контекст, настройки)."""
    # settings_of — вне try: ValidationError (подкласс ValueError) не глушим.
    summary = settings_of(settings).summary
    try:
        return int(getattr(summary, key))
    except (TypeError, ValueError):
        return 0


def _parse_turn_end_int(settings: Any, key: str) -> int:
    value = _parse_summary_int(settings, key)
    return value if value > 0 else 0
