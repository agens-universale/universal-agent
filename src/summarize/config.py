from typing import Any, Dict


def parse_summary_trigger_chars(config_dict: Dict[str, Any]) -> int:
    """
    Порог символов истории для запуска compaction в режиме chat_completions.

    Берётся из config_dict['summary_params']['chat_completions_summary_trigger_chars'].
    При ошибке приведения или отсутствии секции возвращает 0 (compaction отключён).
    """
    summary_params = (
        config_dict.get("summary_params")
        if isinstance(config_dict.get("summary_params"), dict)
        else {}
    )
    summary_trigger_raw = summary_params.get(
        "chat_completions_summary_trigger_chars", 0
    )
    try:
        return int(summary_trigger_raw)
    except (TypeError, ValueError):
        return 0


def parse_summary_turn_end_trigger_chars(config_dict: Dict[str, Any]) -> int:
    """
    Порог turn-end compaction после закрытия turn (``submit_solution``).

    Ожидается уже нормализованное значение в ``summary_params`` после ``load_config``
    / ``resolve_paths``. Парсер только читает int; дефолт 60% mid-turn задаётся в конфиг-модуле.
  """
    summary_params = (
        config_dict.get("summary_params")
        if isinstance(config_dict.get("summary_params"), dict)
        else {}
    )
    turn_end_raw = summary_params.get(
        "chat_completions_summary_turn_end_trigger_chars", 0
    )
    try:
        turn_end = int(turn_end_raw)
    except (TypeError, ValueError):
        return 0
    if turn_end > 0:
        return turn_end
    return 0
