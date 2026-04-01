"""Шаг обогащения памяти: запрос к LLM и разбор `<add_to_memory>`."""

import re
from dataclasses import dataclass
from typing import Any, Dict, List

from summarize import llm_chat


@dataclass
class MemoryAdditionsResult:
    """Результат запроса к LLM для предложений по долговременной памяти диалога.

    Назначение: зафиксировать и извлечённый текст для `thread_notes`, и полезные
    для отладки/трассировки артефакты (сообщения запроса и сырой ответ модели).

    Attributes:
        extracted: Содержимое тега `<add_to_memory>` после парсинга; пустая строка,
            если тега нет или он пустой.
        request_messages: Список сообщений (обычно system + user), отправленный
            в `chat completions` для этого шага.
        raw_response: Полный текст ответа модели до извлечения XML-фрагмента.
    """

    extracted: str
    request_messages: List[Dict[str, str]]
    raw_response: str


def extract_add_to_memory(block: str) -> str:
    """Достаёт содержимое первого парного тега `<add_to_memory>...</add_to_memory>`.

    Назначение: распарсить строго ожидаемый формат ответа шага обогащения памяти.

    Args:
        block: Сырой текст ответа модели.

    Returns:
        Внутренность тега с обрезанными краевыми пробелами или пустая строка,
        если тег не найден.
    """
    match = re.search(r"<add_to_memory>\s*(.*?)\s*</add_to_memory>", block, re.DOTALL | re.IGNORECASE)
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
    """Запрашивает у LLM фрагмент долговременной памяти в формате `<add_to_memory>`.

    Назначение: второй такт после сводки (когда включён): предложить только
    устойчивые факты и решения, не дублируя уже имеющееся в сводке и `current_memory`,
    для последующего добавления в `thread_notes`.

    Поведение: формируется system+user промпт с тремя XML-блоками (история, сводка,
    память); вызывается `llm_chat.request_chat_completion_text`; логируются сырой ответ и
    извлечённое содержимое тега; возвращается `MemoryAdditionsResult`.

    Args:
        ctx: Контекст с конфигом и логгером.
        state: Состояние для выбора модели и запроса к бэкенду.
        history_text: Полный текст диалога в формате, как у `dialog_history.build_history_text`.
        summary_text: Итоговая сводка после шага суммаризации.
        current_memory: Текущее содержимое памяти треда (строка).

    Returns:
        Результат с полями `extracted`, `request_messages`, `raw_response`.
    """
    system = (
        "You are helping enrich long-term session memory for a technical dialogue. "
        "You receive: (1) full dialog history, (2) summary, (3) thread_notes snapshot. "
        "Return EXACTLY one XML tag and nothing else: <add_to_memory>...</add_to_memory>. "
        "Inside the tag, include ONLY durable information that should persist across turns: "
        "stable facts, confirmed decisions, constraints, accepted requirements, key assumptions, "
        "important environment/project details, and explicit user preferences/rules. "
        "Also include meaningful progress state, active plans/next steps, and working hypotheses when they are useful "
        "for continuity and are not already captured in curent_summary/current memory. "
        "Do NOT include transient chatter, noisy tool execution logs, or duplicate/paraphrased content already present "
        "in current_summary/current memory. "
        "Prefer concise bullet points in Markdown. "
        "If there is nothing new worth storing, return an empty tag: <add_to_memory></add_to_memory>."
    )
    user_prompt = (
        "<dialog_history>\n"
        f"{history_text}\n"
        "</dialog_history>\n\n"
        "<current_summary>\n"
        f"{summary_text}\n"
        "</current_summary>\n\n"
        "<thread_notes>\n"
        f"{current_memory}\n"
        "</thread_notes>\n"
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
    logger = getattr(ctx, "logger", None)
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
    extracted = extract_add_to_memory(block)
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
