"""Шаг обновления памяти: запрос к LLM и разбор `<updated_journal>`."""

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from summarize import llm_chat

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MEMORY_UPDATE_PROMPT_PATH = os.path.abspath(
    os.path.join(_BASE_DIR, "..", "prompts", "chat_completions_memory_update_prompt.md")
)


@dataclass
class MemoryAdditionsResult:
    """Результат запроса к LLM для предложений по долговременной памяти диалога.

    Назначение: зафиксировать и извлечённый текст для ``memory_files["journal"]``, и полезные
    для отладки/трассировки артефакты (сообщения запроса и сырой ответ модели).

    Attributes:
    extracted: Содержимое обновлённого journal; для обратной совместимости поле
            оставлено с тем же именем.
        request_messages: Список сообщений (обычно system + user), отправленный
            в `chat completions` для этого шага.
        raw_response: Полный текст ответа модели до извлечения XML-фрагмента.
    """

    extracted: str
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


def suggest_memory_additions(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    summary_text: str,
    current_memory: str,
) -> MemoryAdditionsResult:
    """Запрашивает у LLM актуальную версию journal в формате `<updated_journal>`.

    Назначение: второй такт после сводки (когда включён): переписать `journal`
    целиком с учётом истории + summary + текущей памяти, чтобы избежать накопления
    дубликатов и устаревших промежуточных статусов.

    Поведение: формируется system+user промпт с тремя XML-блоками (история, сводка,
    текущий journal); вызывается `llm_chat.request_chat_completion_text`; логируются
    сырой ответ и извлечённый обновлённый journal; возвращается `MemoryAdditionsResult`.

    Args:
        ctx: Контекст с конфигом и логгером.
        state: Состояние для выбора модели и запроса к бэкенду.
        history_text: Полный текст диалога в формате, как у `dialog_history.build_history_text`.
        summary_text: Итоговая сводка после шага суммаризации.
        current_memory: Текущий journal треда.

    Returns:
        Результат с полями `extracted`, `request_messages`, `raw_response`.
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
    prompt_path = os.path.abspath(prompt_path_raw)
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
    block = llm_chat.request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        max_tokens=1200,
        request_label="memory_additions",
    )
    llm_chat.log_llm_prompt_and_result(
        logger=logger,
        label="memory_additions",
        messages=request_messages,
        output_text=block,
    )
    if logger is not None:
        logger.info(
            "memory_additions_raw_response chars=%s preview=%s",
            len(block or ""),
            llm_chat.shorten_for_log(block or "", max_chars=500),
        )
    extracted = extract_updated_journal(block)
    if logger is not None:
        logger.info(
            "memory_additions_extracted chars=%s preview=%s",
            len(extracted or ""),
            llm_chat.shorten_for_log(extracted or "", max_chars=500),
        )
    return MemoryAdditionsResult(
        extracted=extracted,
        request_messages=request_messages,
        raw_response=block,
    )
