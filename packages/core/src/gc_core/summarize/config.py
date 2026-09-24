from typing import Any, Dict


def parse_summary_trigger_chars(config_dict: Dict[str, Any]) -> int:
    """
    Порог символов истории для запуска compaction в режиме chat_completions.

    Берётся из config_dict['summary_params']['chat_completions_summary_trigger_chars'].
    При ошибке приведения или отсутствии секции возвращает 0 (compaction отключён).
    """
    return _parse_summary_int(config_dict, "chat_completions_summary_trigger_chars")


def parse_summary_trigger_tokens(config_dict: Dict[str, Any]) -> int:
    """Порог input-токенов для mid-turn / pre-run. 0 — плечо выключено."""
    return _parse_summary_int(config_dict, "chat_completions_summary_trigger_tokens")


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
