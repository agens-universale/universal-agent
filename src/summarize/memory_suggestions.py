"""Шаг обновления памяти: запрос к LLM и разбор `<updated_journal>`."""

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from utils.llm_chat_client import (
    log_llm_prompt_and_result,
    request_chat_completion_text,
    shorten_for_log,
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_BASE_DIR, "..", ".."))
DEFAULT_MEMORY_UPDATE_PROMPT_PATH = os.path.abspath(
    os.path.join(_PROJECT_ROOT, "prompts", "chat_completions_memory_update_prompt.md")
)


@dataclass
class UpdatedJournalResult:
    """Результат LLM-шага полной актуализации ``memory_files["journal"]``.

    Назначение: зафиксировать переписанный journal и артефакты запроса (сообщения,
    сырой ответ) для трассировки и отладки.

    Attributes:
        updated_journal: Текст нового journal из ``<updated_journal>...</updated_journal>``;
            при ошибке формата ответа — пустая строка.
        request_messages: Сообщения (обычно system + user), отправленные в chat completions.
        raw_response: Полный текст ответа модели до извлечения XML-фрагмента.
    """

    updated_journal: str
    request_messages: List[Dict[str, str]]
    raw_response: str


def extract_updated_journal(block: str) -> str:
    """Достаёт содержимое первого парного тега `<updated_journal>...</updated_journal>`.

    Назначение: распарсить строго ожидаемый формат ответа шага актуализации journal.

    Args:
        block: Сырой текст ответа модели.

    Returns:
        Внутренность тега с обрезанными краевыми пробелами или пустая строка.
    """
    match = re.search(
        r"<updated_journal>\s*(.*?)\s*</updated_journal>",
        block,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return ""
    return match.group(1).strip()


def suggest_updated_journal(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    summary_text: str,
    current_memory: str,
) -> UpdatedJournalResult:
    """Запрашивает у LLM полную актуальную версию journal в `<updated_journal>`.

    Назначение: второй такт после сводки (когда включён) — не дельта, а **перепись**
    ``journal`` целиком по истории диалога, итоговой сводке и предыдущему journal,
    чтобы убрать дубликаты и устаревшие промежуточные статусы.

    Поведение: system из файла промпта; user с блоками ``<dialog_history>``,
    ``<current_summary>``, ``<current_journal>``; вызов
    ``utils.llm_chat_client.request_chat_completion_text``; логирование; разбор
    ``<updated_journal>`` → поле ``updated_journal`` в ``UpdatedJournalResult``.

    Args:
        ctx: Контекст с конфигом и логгером.
        state: Состояние для выбора модели и запроса к бэкенду.
        history_text: Текст диалога в формате ``dialog_history.build_history_text``.
        summary_text: Итоговая сводка после шага суммаризации.
        current_memory: Текущий journal треда до переписывания.

    Returns:
        ``UpdatedJournalResult`` с полями ``updated_journal``, ``request_messages``, ``raw_response``.
    """
    cfg = getattr(ctx, "config_dict", {}) or {}
    summary_params = cfg.get("summary_params") if isinstance(cfg.get("summary_params"), dict) else {}
    prompt_path_raw = str(
        summary_params.get("chat_completions_memory_update_prompt_path")
        or DEFAULT_MEMORY_UPDATE_PROMPT_PATH
    ).strip()
    if not prompt_path_raw:
        raise ValueError(
            "summary_params.chat_completions_memory_update_prompt_path is empty"
        )
    prompt_path = (
        prompt_path_raw
        if os.path.isabs(prompt_path_raw)
        else os.path.abspath(os.path.join(_PROJECT_ROOT, prompt_path_raw))
    )
    if not os.path.exists(prompt_path):
        raise FileNotFoundError(
            f"Memory update prompt file not found: {prompt_path}"
        )
    try:
        with open(prompt_path, "r", encoding="utf-8") as fd:
            system = fd.read().strip()
    except OSError as prompt_exc:
        raise OSError(
            f"Failed to read memory update prompt file: {prompt_path}"
        ) from prompt_exc
    if not system:
        raise ValueError(f"Memory update prompt file is empty: {prompt_path}")
    logger = getattr(ctx, "logger", None)
    user_prompt = (
        "<dialog_history>\n"
        f"{history_text}\n"
        "</dialog_history>\n\n"
        "<current_summary>\n"
        f"{summary_text}\n"
        "</current_summary>\n\n"
        "<current_journal>\n"
        f"{current_memory}\n"
        "</current_journal>\n"
    )
    request_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    request_label = "journal_update_after_summary"
    raw_llm_response = request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        max_tokens=1200,
        request_label=request_label,
    )
    log_llm_prompt_and_result(
        logger=logger,
        label=request_label,
        messages=request_messages,
        output_text=raw_llm_response,
    )
    if logger is not None:
        logger.info(
            "journal_update_after_summary_raw chars=%s preview=%s",
            len(raw_llm_response or ""),
            shorten_for_log(raw_llm_response or "", max_chars=500),
        )
    updated_journal_text = extract_updated_journal(raw_llm_response)
    if logger is not None:
        logger.info(
            "journal_update_after_summary_parsed chars=%s preview=%s",
            len(updated_journal_text or ""),
            shorten_for_log(updated_journal_text or "", max_chars=500),
        )
    return UpdatedJournalResult(
        updated_journal=updated_journal_text,
        request_messages=request_messages,
        raw_response=raw_llm_response,
    )
