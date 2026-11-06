"""Отдельные LLM-вызовы суммаризации: сжатие и починка текста сводки."""

from typing import Any

from gc_core.summarize.llm import arequest_summary_text
from gc_core.summarize.summary_spec import (
    render_output_shape_for_prompt,
    render_tag_roles_for_prompt,
    summary_body_chars,
)
from gc_core.summarize.summary_validation import (
    SummaryRejectedError,
    validate_summary_text,
)


async def acompress_summary_text(
    *,
    ctx: Any,
    state: Any,
    summary_text: str,
    max_chars: int,
    max_tokens: int,
    trace: dict[str, Any] | None = None,
) -> str:
    """Сжимает слишком длинную сводку вторым вызовом LLM с сохранением смысла.

    Основная (и единственная) реализация шага: синхронный ``compress_summary_text``
    """
    if summary_body_chars(summary_text) <= max_chars:
        return summary_text
    system = (
        "You shorten an existing <summary> XML document. Keep every child tag; "
        "only shorten text inside tags. Do not drop tags, rename tags, or use prose headers."
    )
    request_messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"Compress to at most {max_chars} characters total inside all tags combined.\n\n"
                f"Tag roles (preserve meaning per tag):\n{render_tag_roles_for_prompt()}\n\n"
                f"Output shape:\n{render_output_shape_for_prompt()}\n\n"
                f"<summary_to_compress>\n{summary_text}\n</summary_to_compress>"
            ),
        },
    ]
    text = await arequest_summary_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        request_label="summary_compress",
        consumer="summary",
        max_tokens=max_tokens,
    )
    if trace is not None:
        trace["compression"] = {
            "max_body_chars": max_chars,
            "output_summary_text": text,
        }
    valid, reason = validate_summary_text(text, require_closed_structure=True)
    if not valid:
        raise SummaryRejectedError(
            f"summary compression produced no usable summary ({reason})"
        )
    if summary_body_chars(text) > max_chars:
        raise SummaryRejectedError(
            "summary compression produced no usable summary within character limit"
        )
    return text


async def arepair_summary_text(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    persistent_state_prefix: str,
    invalid_summary: str,
    reason: str,
    missing_tags: tuple[str, ...] = (),
    previous_summary: str = "",
    max_tokens: int,
) -> str:
    """Перегенерирует сводку, если первичный ответ модели не прошёл `validate_summary_text`.

    Основная (и единственная) реализация шага: синхронный ``repair_summary_text``
    """
    system = (
        "You fix an invalid <summary> XML document. Use the inputs below. "
        "Output one complete <summary> with every required child tag filled."
    )
    missing_line = ""
    if missing_tags:
        missing_line = (
            f"Missing child tags (must add): {', '.join(missing_tags)}.\n"
        )
    previous_block = ""
    if (previous_summary or "").strip():
        previous_block = f"<previous_summary>\n{previous_summary.strip()}\n</previous_summary>\n\n"
    user = (
        f"Previous summary is invalid. Reason: {reason}.\n"
        f"{missing_line}\n"
        f"{persistent_state_prefix}"
        f"{previous_block}"
        "<dialog_history>\n"
        f"{history_text}\n"
        "</dialog_history>\n\n"
        f"Tag roles:\n{render_tag_roles_for_prompt()}\n\n"
        f"Output shape:\n{render_output_shape_for_prompt()}\n\n"
        "Rewrite from the inputs above. Do not duplicate unchanged <persistent_state> text.\n\n"
        "<invalid_summary>\n"
        f"{invalid_summary}\n"
        "</invalid_summary>"
    )
    request_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return await arequest_summary_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        request_label="summary_repair",
        consumer="summary",
        max_tokens=max_tokens,
    )
