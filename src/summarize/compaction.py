from typing import Any, Callable, Dict, List, Optional

from message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func

from summarize.summary_serialization import (
    calc_history_chars_for_trigger,
    calc_messages_chars_total,
)
from summarize.summary_message_body import build_dialog_summary_content
from summarize.thread_summarizer import summarize_dialog_via_chat_completions


def _journal_text(state: Any) -> str:
    """Extract journal text from state.shared.memory_files for backward-compat with summarizer."""
    shared = getattr(state, "shared", None)
    if shared is not None:
        mf = getattr(shared, "memory_files", None)
        if isinstance(mf, dict):
            return (mf.get("journal") or "").strip()
    return ""


def without_system_messages(messages_list: List[dict]) -> List[dict]:
    out: List[dict] = []
    for msg in messages_list:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).strip().lower() == "system":
            continue
        out.append(msg)
    return out


def apply_summary_state_patch(
    *,
    state: Any,
    raw_summary: str,
    last_dialog_steps: str,
    updated_journal: str,
    summarized_messages_count: int,
) -> str:
    """
    Атомарно вносит в объект ``state`` результат compaction-суммаризации (без I/O и без решения «когда»).

    Зачем: каноничное состояние исполнения живёт в ``state`` (см. принципы слоёв); эта функция — точка,
    где согласованно обновляются заметки, оффсет шагов, привязка к предыдущему ответу API и список
    сообщений. Вызывается из ``run_summary_compaction`` после успешной суммаризации.

    Что делает:
        1. ``state.shared.memory_files["journal"]`` — полностью обновляет journal значением
           ``updated_journal``, если оно непустое (это уже актуализированная версия, не дельта);
           иначе не меняет.
        2. Собирает тело сообщения-резюме через ``build_dialog_summary_content`` уже **после** шага (1):
           в ``<memory_files>`` попадает актуальный ``journal`` целиком и индекс прочих регистров
           (имя, размер), без полного текста остальных файлов.
        3. ``state.dialog_steps_offset`` — увеличивает на ``summarized_messages_count`` (служебно через
           ``max(0, int(...))``).
        4. ``state.previous_response_id`` — сбрасывает в ``None`` (контракт после compaction).
        5. ``state.messages`` — заменяет на компактный трейс: если в прежней истории есть ``system``,
           оставляет первое такое сообщение первым, затем одно техническое user-сообщение dialog summary
           (``wrap_technical_user_content`` + ``TECHNICAL_TYPE_DIALOG_SUMMARY``).

    Аргументы:
        state:
            Изменяемый in-place объект исполнения; ожидаются атрибуты ``memory_files``, ``dialog_steps_offset``,
            ``previous_response_id``, ``messages``.
        raw_summary:
            Текст сводки LLM для секции ``<summary>``.
        last_dialog_steps:
            Отформатированный хвост диалога для ``<last_dialog_steps>``.
        updated_journal:
            Актуализированная полная версия journal; при непустом значении заменяет
            ``memory_files["journal"]``.
        summarized_messages_count:
            Число сообщений, схлопнутых в этом проходе; участвует в ``dialog_steps_offset``.

    Возвращает:
        Итоговое тело резюме (то же, что вложено в служебное сообщение).
    """
    updated_journal_text = str(updated_journal or "").strip()
    shared = getattr(state, "shared", None)
    if shared is None:
        from states import SharedStateData
        shared = SharedStateData()
        state.shared = shared
    mf = shared.memory_files
    if not isinstance(mf, dict):
        mf = {}
        shared.memory_files = mf
    if updated_journal_text:
        mf["journal"] = updated_journal_text

    summary_message_content = build_dialog_summary_content(
        summary_text=str(raw_summary or ""),
        last_dialog_steps=str(last_dialog_steps or ""),
        memory_files=mf,
        user_messages_log=list(getattr(state, "user_messages_log", []) or []),
    )
    prev_offset = int(getattr(state, "dialog_steps_offset", 0) or 0)
    state.dialog_steps_offset = prev_offset + max(0, int(summarized_messages_count or 0))
    state.previous_response_id = None

    summary_message = {
        "role": "user",
        "content": wrap_technical_user_content(
            (
                "Summarized context from previous dialogue.\n"
                f"{summary_message_content.strip()}"
            ),
            technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
        ),
        "id": generate_message_id_func("m_sum"),
    }
    previous_messages = (
        list(getattr(state, "messages", []))
        if isinstance(getattr(state, "messages", []), list)
        else []
    )
    system_message = None
    for msg in previous_messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).strip().lower() == "system":
            system_message = msg
            break
    if isinstance(system_message, dict):
        state.messages = [system_message, summary_message]
    else:
        state.messages = [summary_message]
    return summary_message_content


def history_exceeds_summary_trigger(
    *,
    state: Any,
    summary_trigger_chars: int,
) -> bool:
    """
    Дешёвая проверка: объём истории (`calc_history_chars_for_trigger`) строго больше порога.

    При ``summary_trigger_chars <= 0`` всегда ``False`` (суммаризация отключена конфигом).
    """
    if summary_trigger_chars <= 0:
        return False
    full_messages = without_system_messages(getattr(state, "messages", []))
    return calc_history_chars_for_trigger(full_messages) > summary_trigger_chars


def run_summary_compaction(
    *,
    ctx: Any,
    state: Any,
    trigger_reason: str,
    is_cli_mode: bool,
    on_summary_applied: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    """
    Выполняет compaction: LLM-сводка и ``apply_summary_state_patch``. Порог «когда» не проверяет —
    вызывающий обязан вызвать после `history_exceeds_summary_trigger` (или в тестах принудительно).
    """
    logger = getattr(ctx, "logger", None)
    thread_id = getattr(state, "thread_id", None)
    full_messages = without_system_messages(getattr(state, "messages", []))
    trigger_volume = calc_history_chars_for_trigger(full_messages)
    serialized_chars = calc_messages_chars_total(full_messages)
    if logger is not None:
        logger.info(
            "summary_triggered reason=%s thread_id=%s trigger_volume=%s serialized_chars=%s history_messages=%s",
            trigger_reason,
            thread_id,
            trigger_volume,
            serialized_chars,
            len(full_messages),
        )
    if is_cli_mode:
        print(
            "[summary] Запущена суммаризация: "
            f"thread={thread_id}, trigger_volume={trigger_volume}, "
            f"serialized_chars={serialized_chars}"
        )

    source_user_log = getattr(state, "user_messages_log", None)
    source_user_log = (
        [str(x) for x in source_user_log if isinstance(x, str)]
        if isinstance(source_user_log, list)
        else []
    )
    summary_result = summarize_dialog_via_chat_completions(
        ctx=ctx,
        state=state,
        full_history_messages=full_messages,
        current_memory=_journal_text(state),
        user_messages_log=source_user_log,
        steps_offset=int(getattr(state, "dialog_steps_offset", 0) or 0),
    )
    raw_summary = str(summary_result.get("raw_summary") or "")
    updated_journal = summary_result.get("updated_journal", "")
    if not updated_journal:
        updated_journal = summary_result.get("memory_additions", "")
    if logger is not None:
        logger.info("summary_received_raw_text=\n%s", raw_summary)
    if is_cli_mode:
        print("[summary] Полученный raw summary:")
        print(raw_summary)

    summary_message_content = apply_summary_state_patch(
        state=state,
        raw_summary=raw_summary,
        last_dialog_steps=str(summary_result.get("last_dialog_steps") or ""),
        updated_journal=updated_journal,
        summarized_messages_count=len(full_messages),
    )
    trace = summary_result.get("trace")
    if isinstance(trace, dict):
        final_out = trace.get("final_output")
        if isinstance(final_out, dict):
            final_out["summary_message_content"] = summary_message_content
    if logger is not None:
        logger.info("summary_received_message_content=\n%s", summary_message_content)

    _shared = getattr(state, "shared", None)
    summary_payload = {
        "summary_message_content": summary_message_content,
        "shared": {
            "memory_files": dict(getattr(_shared, "memory_files", None) or {}) if _shared else {},
            "notes_md": str(getattr(_shared, "notes_md", "") or "") if _shared else "",
        },
        "user_messages_log": list(getattr(state, "user_messages_log", []) or []),
        "dialog_steps_offset": int(getattr(state, "dialog_steps_offset", 0) or 0),
    }
    if on_summary_applied is not None:
        on_summary_applied(
            {
                "trigger_reason": trigger_reason,
                "summary_payload": summary_payload,
                "summary_trace": summary_result.get("trace", {}),
                "newly_summarized_message_id": str(
                    state.messages[0].get("id")
                    if isinstance(getattr(state, "messages", []), list) and state.messages
                    else ""
                ),
            }
        )

    new_serialized = calc_messages_chars_total(
        without_system_messages(getattr(state, "messages", []))
    )
    ratio = (
        round((new_serialized / serialized_chars), 4) if serialized_chars > 0 else 0.0
    )
    if logger is not None:
        logger.info(
            "summary_completed reason=%s thread_id=%s trigger_volume=%s serialized_before=%s serialized_after=%s compression_ratio=%s num_batches=%s updated_journal_chars=%s",
            trigger_reason,
            thread_id,
            trigger_volume,
            serialized_chars,
            new_serialized,
            ratio,
            summary_result.get("num_batches"),
            len(updated_journal or ""),
        )
    if is_cli_mode:
        print(
            "[summary] Завершена: "
            f"serialized_before={serialized_chars}, serialized_after={new_serialized}, ratio={ratio}, "
            f"batches={summary_result.get('num_batches')}, "
            f"updated_journal_chars={len(updated_journal or '')}"
        )


def maybe_run_summary_compaction(
    *,
    ctx: Any,
    state: Any,
    summary_trigger_chars: int,
    trigger_reason: str,
    is_cli_mode: bool,
    on_summary_applied: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> bool:
    """
    Условно выполняет compaction: проверка `history_exceeds_summary_trigger`, затем при необходимости
    `run_summary_compaction`.
    """
    logger = getattr(ctx, "logger", None)
    thread_id = getattr(state, "thread_id", None)
    if summary_trigger_chars <= 0:
        return False
    full_messages = without_system_messages(getattr(state, "messages", []))
    trigger_volume = calc_history_chars_for_trigger(full_messages)
    serialized_chars = calc_messages_chars_total(full_messages)
    if logger is not None:
        logger.info(
            "summary_precheck reason=%s thread_id=%s history_messages=%s trigger_volume=%s serialized_chars=%s threshold=%s",
            trigger_reason,
            thread_id,
            len(full_messages),
            trigger_volume,
            serialized_chars,
            summary_trigger_chars,
        )
    if trigger_volume <= summary_trigger_chars:
        return False

    run_summary_compaction(
        ctx=ctx,
        state=state,
        trigger_reason=trigger_reason,
        is_cli_mode=is_cli_mode,
        on_summary_applied=on_summary_applied,
    )
    return True
