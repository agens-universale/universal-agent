"""Отдельные LLM-вызовы суммаризации: сжатие и починка текста сводки."""

from typing import Any

from summarize import dialog_history
from utils.llm_chat_client import log_llm_prompt_and_result, request_chat_completion_text


def compress_summary_text(
    *,
    ctx: Any,
    state: Any,
    summary_text: str,
    max_chars: int,
    max_tokens: int,
) -> str:
    """Сжимает слишком длинную сводку вторым вызовом LLM с сохранением смысла.

    Назначение: уложить накопленную инкрементальную сводку в `max_accumulated_summary_chars`
    без механической обрезки посередине абзаца, когда это возможно.

    Поведение: если текст уже короче лимита — возвращается как есть. Иначе
    запрос с инструкцией сжать; при пустом ответе — запасной вариант: срез исходника
    до `max_chars`; если ответ длиннее лимита — `dialog_history.truncate_text`.

    Args:
        ctx: Контекст запроса.
        state: Состояние для бэкенда.
        summary_text: Текущая длинная сводка.
        max_chars: Целевая верхняя граница длины результата.
        max_tokens: Лимит токенов на ответ при сжатии.

    Returns:
        Сжатый или усечённый текст, пригодный как новая `accumulated_summary`.
    """
    if not summary_text or len(summary_text) <= max_chars:
        return summary_text
    system = (
        "You are a summarization assistant. Compress the given summary while preserving "
        "all critical facts, constraints, decisions, and technical references. "
        "Output only compressed summary text."
    )
    request_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": summary_text},
    ]
    text = request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        max_tokens=max_tokens,
        request_label="summary_compress",
    )
    log_llm_prompt_and_result(
        logger=getattr(ctx, "logger", None),
        label="summary_compress",
        messages=request_messages,
        output_text=text,
    )
    text = (text or "").strip()
    if not text:
        return summary_text[:max_chars]
    if len(text) > max_chars:
        return dialog_history.truncate_text(text, max_chars)
    return text


def repair_summary_text(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    user_requests_text: str,
    invalid_summary: str,
    reason: str,
    max_tokens: int,
    project_data_block: str = "",
) -> str:
    """Перегенерирует сводку, если первичный ответ модели не прошёл `validate_summary_text`.

    Назначение: повысить надёжность конвейера: дать LLM явный контекст (лог запросов,
    история, причина отказа, черновик) и требование структуры с секциями.

    Args:
        ctx: Контекст выполнения.
        state: Состояние сессии.
        history_text: Текст истории батча, на котором сорвалась валидация.
        user_requests_text: Отформатированный список пользовательских запросов.
        invalid_summary: Исходный невалидный ответ.
        reason: Код от `validate_summary_text` для включения в user-промпт.
        max_tokens: Лимит длины исправленной сводки.
        project_data_block: Фрагмент `<project_data>...</project_data>` (как в основном батче суммаризации).

    Returns:
        Текст исправленной сводки (может снова быть невалидным — проверяет вызывающий).
    """
    system = (
        "You are fixing an invalid technical summary for a software-engineering dialogue. "
        "You receive the original user requests log, full dialogue history, and an invalid summary draft. "
        "Produce a corrected summary that is concise, complete, structured, and actionable."
    )
    user = (
        f"Previous summary is invalid. Reason: {reason}.\n\n"
        "<user_requests_log>\n"
        f"{user_requests_text}\n"
        "</user_requests_log>\n\n"
        "<dialog_history>\n"
        f"{history_text}\n"
        "</dialog_history>\n\n"
        f"{project_data_block}"
        "Rewrite summary using the real context above. It must be high-quality, concise, and structured. "
        "Use these section headers (exact UPPERCASE_WITH_UNDERSCORES or Markdown headings with the same words): "
        "TASK_AND_CONTEXT, GOALS, DECISIONS_AND_RATIONALE, PLAN_AND_PROGRESS, NEXT_ACTIONS, "
        "SPEC_AND_CONSTRAINTS, RISKS_AND_OPEN_QUESTIONS, IMPORTANT_ARTIFACTS.\n\n"
        "<invalid_summary>\n"
        f"{invalid_summary}\n"
        "</invalid_summary>"
    )
    request_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    out = request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        max_tokens=max_tokens,
        request_label="summary_repair",
    )
    log_llm_prompt_and_result(
        logger=getattr(ctx, "logger", None),
        label="summary_repair",
        messages=request_messages,
        output_text=out,
    )
    return out
