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
