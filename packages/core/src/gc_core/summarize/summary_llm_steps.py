"""Отдельные LLM-вызовы суммаризации: сжатие и починка текста сводки."""

from typing import Any

from gc_core.message_format import dialog_history as dialog_history
from gc_core.summarize.llm import arequest_summary_text
from gc_core.summarize.summary_spec import render_output_shape_for_prompt, render_tag_roles_for_prompt




async def acompress_summary_text(
    *,
    ctx: Any,
    state: Any,
    summary_text: str,
    max_chars: int,
    max_tokens: int,
) -> str:
    """Сжимает слишком длинную сводку вторым вызовом LLM с сохранением смысла.

    Основная (и единственная) реализация шага: синхронный ``compress_summary_text``
    — шим над ней через портал. Ждёт aux-LLM без потока из пула.
    """
    if not summary_text or len(summary_text) <= max_chars:
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
    if not text:
        return summary_text[:max_chars]
    if len(text) > max_chars:
        return dialog_history.truncate_text(text, max_chars)
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
    — шим над ней через портал. Ждёт aux-LLM без потока из пула.
    """
    system = (
        "You fix an invalid <summary> XML document. Use the inputs below. "
        "Output one complete <summary> with every required child tag filled."
    )
    missing_line = ""
    if missing_tags:
        missing_line = f"Missing child tags (must add): {', '.join(missing_tags)}.\n"
    previous_block = ""
    if (previous_summary or "").strip():
        previous_block = (
            "<previous_summary>\n"
            f"{previous_summary.strip()}\n"
            "</previous_summary>\n\n"
        )
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


# --- синхронные двойники ---------------------------------------------------
#
# Имена сохранены для вызывающих вне цикла агента (ручной /compact, тесты).
# Портал переиспользует один loop — значит один httpx-клиент и живой пул
# соединений; изнутри асинхронного цикла звать их нельзя (портал поднимет
# SyncApiInsideEventLoopError).


def compress_summary_text(*args, **kwargs):
    """Синхронный двойник. Реализация одна — асинхронная; здесь только портал."""
    from gc_core.utils.portal import get_portal

    return get_portal().run(acompress_summary_text(*args, **kwargs))


def repair_summary_text(*args, **kwargs):
    """Синхронный двойник. Реализация одна — асинхронная; здесь только портал."""
    from gc_core.utils.portal import get_portal

    return get_portal().run(arepair_summary_text(*args, **kwargs))
