"""Перефраз итога хода в форму из файла.

Шаг gc-cli после конца хода: принятая проза или аргументы
``turn_finish`` как есть, один служебный aux-запрос, текст наружу.
Тред и цикл агента этот модуль не меняет.
"""

import json
import sys
from pathlib import Path
from typing import Any

from gc_core.config_modules.chat_completions import (
    DEFAULT_AUXILIARY_LLM_MAX_TOKENS,
)
from gc_core.summarize.llm import (
    arequest_auxiliary_completion_text,
    auxiliary_llm_completion_kwargs,
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
    """Один aux-запрос. Пустой ответ и сбой транспорта — ошибка шага."""
    if terminal_outcome or state is None:
        raise StructuredOutputError(_no_outcome_message(terminal_outcome))
    outcome = turn_outcome_text(response)
    if outcome is None:
        raise StructuredOutputError(_no_outcome_message(None))
    max_tokens = _auxiliary_max_tokens(ctx)
    user = f"Format description:\n{format_text}\n\nTurn outcome:\n{outcome}"
    try:
        text = await arequest_auxiliary_completion_text(
            ctx=ctx,
            state=state,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            request_label=REQUEST_LABEL,
            consumer=_CONSUMER,
            payload_overrides=auxiliary_llm_completion_kwargs(ctx),
            logger=getattr(ctx, "logger", None),
        )
    except Exception as exc:
        raise StructuredOutputError(
            f"structured-output-format: запрос перефраза не удался: {exc}"
        ) from exc
    rendered = str(text or "").strip()
    if not rendered:
        raise StructuredOutputError(
            "structured-output-format: пустой ответ перефраза"
        )
    return rendered


async def aemit_structured_result(
    *,
    response: Any,
    config_dict: dict[str, Any],
    ctx: Any,
    state: Any,
    terminal_outcome: str | None,
    format_text: str,
) -> int:
    """Напечатать перефраз и записать ``final_answer``. 0 или 1.

    В ``logging_format: jsonl`` отдельной строки в stdout нет: поток
    событий уже занимает его, текст живёт в событии лога.
    """
    logger = getattr(ctx, "logger", None)
    try:
        rendered = await arephrase_turn_outcome(
            format_text=format_text,
            response=response,
            ctx=ctx,
            state=state,
            terminal_outcome=terminal_outcome,
        )
    except StructuredOutputError as exc:
        print(f"[gc] {exc}", file=sys.stderr)
        if logger is not None:
            logger.warning("%s", exc)
        return 1
    if not _stdout_is_event_stream(config_dict):
        print(rendered)
    if logger is not None:
        logger.info(
            "final_answer",
            extra={"event": "final_answer", "answer": rendered},
        )
    return 0


def _auxiliary_max_tokens(ctx: Any) -> int:
    """Потолок ответа. Нет числа в профиле — дефолт aux, ход уже сделан."""
    auxiliary = getattr(ctx, "auxiliary_llm", None)
    max_tokens = getattr(auxiliary, "max_tokens", None)
    if isinstance(max_tokens, int) and max_tokens >= 1:
        return max_tokens
    return DEFAULT_AUXILIARY_LLM_MAX_TOKENS


def _no_outcome_message(terminal_outcome: str | None) -> str:
    if terminal_outcome:
        return (
            "structured-output-format: ход завершился без итога "
            f"({terminal_outcome})"
        )
    return "structured-output-format: ход завершился без итога"


def _stdout_is_event_stream(config_dict: dict[str, Any]) -> bool:
    return (
        str(config_dict.get("logging_format", "text")).strip().lower()
        == "jsonl"
    )
