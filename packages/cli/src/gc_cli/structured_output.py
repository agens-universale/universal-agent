"""CLI rendering for the shared post-run structured output step."""

import sys
from typing import Any

from gc_core.settings import settings_of
from gc_core.structured_output import (
    REQUEST_LABEL,  # noqa: F401  # public compatibility
    StructuredOutputError,
    arephrase_turn_outcome,
    read_format_description,  # noqa: F401
    turn_outcome_text,  # noqa: F401
)
from gc_core.utils.json_text import format_json_for_display


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

    Успешный текст попадает в stdout. Если туда уже пишет jsonl-хендлер,
    сырая строка сломала бы разбор по строкам: хватает события
    ``final_answer`` с перефразом. Иначе печатается сам текст.

    Отказ перефраза ``final_answer`` не отменяет: в лог сначала уходит итог
    хода, затем ``structured_output_failed``. Формы в stdout нет. Причина
    пишется в stderr, код выхода — 1.
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
        _log_final_answer(logger, format_json_for_display(response))
        _log_structured_output_failed(logger, exc)
        print(f"[gc] {exc}", file=sys.stderr)
        return 1
    if not _stdout_is_jsonl_stream(settings_of(config_dict)):
        print(rendered)
    _log_final_answer(logger, rendered)
    return 0


def _log_final_answer(logger: Any, answer: str) -> None:
    """Итог хода в лог. Пустая строка — не ответ, событие не пишется."""
    if logger is None or not answer:
        return
    logger.info(
        "final_answer",
        extra={"event": "final_answer", "answer": answer},
    )


def _log_structured_output_failed(
    logger: Any, exc: StructuredOutputError
) -> None:
    if logger is None:
        return
    logger.warning(
        "%s",
        exc,
        extra={"event": "structured_output_failed", "error": str(exc)},
    )


def _stdout_is_jsonl_stream(settings: Any) -> bool:
    """Хендлер jsonl на stdout, как в ``_build_jsonl_logger``.

    Он вешается только при ``logging.format: jsonl`` и ``console_trace``.
    """
    logging_format = str(settings.logging.format or "text")
    if logging_format.strip().lower() != "jsonl":
        return False
    return bool(settings.logging.console_trace)
