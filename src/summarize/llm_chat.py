"""Вызов chat completions к бэкенду и сопутствующее логирование промпта/ответа."""

from typing import Any, Dict, List

from utils.connect_backend import (
    create_call_headers,
    request_backend_chat_completions,
)


def shorten_for_log(text: str, max_chars: int = 500) -> str:
    """Усекает текст для компактного логирования с пометкой о скрытом хвосте.

    Args:
        text: Строка для превью; ``None`` трактуется как пустая.
        max_chars: Максимальная длина возвращаемого префикса (после `strip`).

    Returns:
        Полный текст, если он не длиннее `max_chars`, иначе префикс и суффикс
        ``... [TRUNCATED N chars]``, где N — число отброшенных символов.
    """
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    return f"{s[:max_chars]} ... [TRUNCATED {len(s) - max_chars} chars]"


def log_llm_prompt_and_result(
    *,
    logger: Any,
    label: str,
    messages: List[Dict[str, str]],
    output_text: str,
) -> None:
    """Пишет в лог полный system/user промпт и ответ LLM для шага суммаризации или починки.

    Назначение: трассировка без повторной сборки payload вручную при отладке качества
    сводок.

    Args:
        logger: Обычно `ctx.logger`; при ``None`` вызов — no-op.
        label: Короткий тег шага (например ``summary_batch_1``, ``memory_additions``).
        messages: Сообщения запроса к API с ролями `system` и/или `user`.
        output_text: Текст ответа модели (может быть пустым).

    Returns:
        None.
    """
    if logger is None:
        return
    system_part = "\n\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    )
    user_part = "\n\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "user"
    )
    logger.info(
        "summary_llm_prompt label=%s system=\n%s\n\nuser=\n%s",
        label,
        system_part,
        user_part,
    )
    logger.info("summary_llm_result label=%s output=\n%s", label, output_text or "")


def request_chat_completion_text(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    max_tokens: int,
    request_label: str = "chat_completion",
) -> str:
    """Отправляет запрос chat completions на бэкенд и возвращает текст ответа ассистента.

    Назначение: единая точка HTTP-вызова для всех LLM-шагов суммаризации
    (суммаризация, сжатие, починка, память) с фиксированными гиперпараметрами
    и без инструментов.

    Поведение: собирается payload (модель из `state.default_model` или конфига,
    `temperature`≈0, `tools=[]`, лимит `max_tokens`), выставляются заголовки
    `create_call_headers`, вызывается `request_backend_chat_completions`; из
    ответа извлекается `choices[0].message.content`.

    Args:
        ctx: Контекст выполнения с `config_dict` и опционально `logger`.
        state: Состояние сессии (модель, данные для заголовков).
        messages: Пары роль/контент для chat API.
        max_tokens: Верхняя граница длины генерируемого ответа.
        request_label: Метка для возможной трассировки на стороне вызывающего;
            в этом теле не используется, но фиксирует семантику вызова.

    Returns:
        Обрезанный по краям текст ответа или пустая строка при сбое/неожиданном
        формате ответа.
    """
    cfg = getattr(ctx, "config_dict", {}) or {}
    payload = {
        "model": getattr(state, "default_model", None) or cfg.get("model"),
        "messages": messages,
        "tools": [],
        "temperature": 0.01,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "max_tokens": max_tokens,
        "repetition_penalty": 1.1,
        "separate_reasoning": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = create_call_headers(state, ctx)
    res = request_backend_chat_completions(
        payload,
        headers,
        url=cfg["completions_url"],
        verify=cfg.get("use_ssl", True),
        use_retry=cfg.get("use_retry", False),
        logger=getattr(ctx, "logger", None),
    )
    text = (
        res.get("choices", [{}])[0].get("message", {}).get("content")
        if isinstance(res, dict)
        else ""
    )
    return (text or "").strip()
