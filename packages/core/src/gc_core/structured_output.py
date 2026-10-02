"""Shared post-run rephrasing and client-side format preflight."""

import json
from pathlib import Path
from typing import Any

from gc_core.config_modules.chat_completions import (
    DEFAULT_AUXILIARY_LLM_MAX_TOKENS,
)
from gc_core.config_modules.strip_tags_projection import (
    projection_tags_from_config,
)
from gc_core.execution.contracts import ExecutionFailure
from gc_core.message_projection import project_text_for_model
from gc_core.summarize.llm import auxiliary_llm_completion_kwargs
from gc_core.utils.backend_errors import (
    FINISH_KIND_LENGTH,
    classify_finish_kind,
    finish_prevents_text_final,
)
from gc_core.utils.llm_chat_client import (
    arequest_chat_completion_outcome,
    log_llm_prompt_and_result,
)

REQUEST_LABEL = "structured_output"


_CONSUMER = "summary"


_SYSTEM_PROMPT = (
    "Rephrase the given turn outcome into the format from the format "
    "description.\n"
    "Use only facts present in the turn outcome. When the format "
    "asks for something the outcome does not contain, leave that spot "
    "empty the way the format describes (null, a dash, or omit the "
    "key).\n"
    "Language: if the format description names a language, write in "
    "that language. Otherwise write in the same language as the turn "
    "outcome.\n"
    "Return only the formatted text, with no preamble."
)


class StructuredOutputError(Exception):
    """Ожидаемый отказ шага. Текст — для stderr, в stdout формы нет."""


def read_format_description(path: str) -> str:
    """Прочитать файл формы до старта агента.

    Относительный путь считается от текущей папки запуска. Пустой путь,
    нечитаемый файл и пустое содержимое — ошибка.
    """
    raw = str(path or "").strip()
    if not raw:
        raise StructuredOutputError(
            "structured-output-format: путь к файлу пустой"
        )
    file_path = Path(raw).expanduser().resolve()
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StructuredOutputError(
            f"structured-output-format: файл не читается: {file_path} ({exc})"
        ) from exc
    if not text.strip():
        raise StructuredOutputError(
            f"structured-output-format: файл пустой: {raw}"
        )
    return text


def turn_outcome_text(response: Any) -> str | None:
    """Текст итога хода как его вернул цикл, без разбора полей.

    Принятая проза — как есть. Аргументы ``turn_finish`` — сырой JSON
    или уже словарь. Пустое значение — не итог.
    """
    if response is None:
        return None
    if isinstance(response, str):
        text = response.strip()
        return text or None
    if isinstance(response, (dict, list)):
        return json.dumps(response, ensure_ascii=False, indent=2)
    text = str(response).strip()
    return text or None


async def arephrase_turn_outcome(
    *,
    format_text: str,
    response: Any,
    ctx: Any,
    state: Any,
    terminal_outcome: str | None,
) -> str:
    """Один aux-запрос. Пустой ответ, обрыв, отказ и сбой — ошибка шага.

    Обрыв — ``finish_reason=length``. Отказ — ``content_filter``,
    ``refusal``, ``safety``. ``stop`` и незнакомая причина при непустом
    тексте принимаются: файл формы не схема, тело не разбирается.
    """
    if terminal_outcome or state is None:
        raise StructuredOutputError(_no_outcome_message(terminal_outcome))
    outcome = turn_outcome_text(response)
    if outcome is None:
        raise StructuredOutputError(_no_outcome_message(None))
    max_tokens = _auxiliary_max_tokens(ctx)
    user = f"Format description:\n{format_text}\n\nTurn outcome:\n{outcome}"
    try:
        text, finish_reason = await _arequest_rephrase(
            ctx=ctx,
            state=state,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
        )
    except ExecutionFailure:
        raise
    except Exception as exc:
        raise StructuredOutputError(
            f"structured-output-format: запрос перефраза не удался: {exc}"
        ) from exc
    if finish_prevents_text_final(finish_reason):
        raise StructuredOutputError(
            _unfinished_rephrase_message(finish_reason)
        )
    rendered = str(text or "").strip()
    if not rendered:
        raise StructuredOutputError(
            "structured-output-format: пустой ответ перефраза"
        )
    return rendered


def _auxiliary_max_tokens(ctx: Any) -> int:
    """Потолок ответа. Нет числа в профиле — дефолт aux, ход уже сделан."""
    auxiliary = getattr(ctx, "auxiliary_llm", None)
    max_tokens = getattr(auxiliary, "max_tokens", None)
    if isinstance(max_tokens, int) and max_tokens >= 1:
        return max_tokens
    return DEFAULT_AUXILIARY_LLM_MAX_TOKENS


async def _arequest_rephrase(
    *,
    ctx: Any,
    state: Any,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> tuple[str, str | None]:
    """Служебный запрос перефраза: текст после проекции и причина остановки.

    Грязный ответ пишется в лог. Наружу — текст без ``<think>`` и тегов из
    ``strip_tags_projection``, как у прочих служебных вызовов. Причина
    остановки берётся из сырого choice.
    """
    outcome = await arequest_chat_completion_outcome(
        ctx=ctx,
        state=state,
        messages=messages,
        max_tokens=max_tokens,
        request_label=REQUEST_LABEL,
        consumer=_CONSUMER,
        payload_overrides=auxiliary_llm_completion_kwargs(ctx),
    )
    logger = getattr(ctx, "logger", None)
    log_llm_prompt_and_result(
        logger=logger,
        label=REQUEST_LABEL,
        messages=messages,
        output_text=outcome.text,
    )
    tags = projection_tags_from_config(ctx)
    projected = project_text_for_model(
        outcome.text, tags=tags, strip_think=True
    )
    return projected, outcome.finish_reason


def _unfinished_rephrase_message(finish_reason: str | None) -> str:
    """Обрыв по длине и отказ провайдера. Фрагмент наружу не отдаётся."""
    shown = finish_reason if isinstance(finish_reason, str) else ""
    if classify_finish_kind(finish_reason) == FINISH_KIND_LENGTH:
        return (
            "structured-output-format: ответ перефраза оборван "
            f"(finish_reason={shown})"
        )
    return (
        "structured-output-format: ответ перефраза отклонён "
        f"(finish_reason={shown})"
    )


def _no_outcome_message(terminal_outcome: str | None) -> str:
    if terminal_outcome:
        return (
            "structured-output-format: ход завершился без итога "
            f"({terminal_outcome})"
        )
    return "structured-output-format: ход завершился без итога"
