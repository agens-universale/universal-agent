from typing import Any, Callable, Dict, List, Optional

from summarize.summary_utils import apply_summary_state_patch
from summarize.thread_summarizer import summarize_dialog_via_chat_completions
from thread_store import calc_messages_chars_total


def without_system_messages(messages_list: List[dict]) -> List[dict]:
    out: List[dict] = []
    for msg in messages_list:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).strip().lower() == "system":
            continue
        out.append(msg)
    return out


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
    Условно выполняет compaction диалога через единый runtime-state.

    Назначение:
    - Проверить, пора ли суммаризировать текущую сессию по порогу символов.
    - Если порог достигнут, получить summary через LLM и атомарно применить patch к `state`.
    - Передать результат наружу через callback для побочных эффектов (archive/trace/persistence),
      не выполняя I/O внутри pipeline.

    Ключевой архитектурный принцип:
    - Источник данных для суммаризации всегда один: `state.messages`.
    - Флаг `-t` не меняет входные данные и логику compaction; он влияет только на post-action
      в orchestration-слое (через `on_summary_applied`).

    Алгоритм:
    1) Берем `state.messages` и отбрасываем `system`-сообщения.
    2) Считаем общий объем текста (`chars_total`) и сравниваем с `summary_trigger_chars`.
       Если порог не достигнут -> `False`.
    3) Вызываем `summarize_dialog_via_chat_completions(...)` и получаем summary payload.
    4) Применяем результат к состоянию через `apply_summary_state_patch(...)`:
       - обновление `notes_md` (при наличии additions),
       - инкремент `dialog_steps_offset`,
       - `previous_response_id = None`,
       - схлопывание диалоговой истории до одного технического summary user-сообщения
         с сохранением `system`-сообщения (если оно присутствовало).
    5) Если передан `on_summary_applied`, отдаем в него событие с payload/trace/metadata
       для внешних побочных действий.
    6) Логируем метрики и возвращаем `True`.

    Параметры:
    - `ctx`: run context (конфиг, logger и прочее окружение вызова).
    - `state`: runtime session/state; ожидаются поля `messages`, `notes_md`,
      `user_messages_log`, `dialog_steps_offset`, `thread_id`.
    - `summary_trigger_chars`: порог символов для запуска суммаризации.
    - `trigger_reason`: причина вызова (например, `pre_run` или `post_step`) для логов/trace.
    - `is_cli_mode`: включает человекочитаемый консольный вывод прогресса суммаризации.
    - `on_summary_applied`: необязательный callback post-action.
      Вызывается после успешного apply patch и получает словарь события:
      {
        "trigger_reason": str,
        "summary_payload": {
          "summary_message_content": str,
          "notes_md": str,
          "user_messages_log": list[str],
          "dialog_steps_offset": int,
        },
        "summary_trace": dict,
        "newly_summarized_message_id": str,
      }

    Возвращает:
    - `True`, если compaction выполнен.
    - `False`, если порог не достигнут и compaction пропущен.
    """
    logger = getattr(ctx, "logger", None)
    thread_id = getattr(state, "thread_id", None)
    full_messages = without_system_messages(getattr(state, "messages", []))
    chars_total = calc_messages_chars_total(full_messages)
    if logger is not None:
        logger.info(
            "summary_precheck reason=%s thread_id=%s history_messages=%s chars_total=%s threshold=%s",
            trigger_reason,
            thread_id,
            len(full_messages),
            chars_total,
            summary_trigger_chars,
        )
    if chars_total < summary_trigger_chars:
        return False

    if logger is not None:
        logger.info(
            "summary_triggered reason=%s thread_id=%s chars_total=%s threshold=%s",
            trigger_reason,
            thread_id,
            chars_total,
            summary_trigger_chars,
        )
    if is_cli_mode:
        print(
            "[summary] Запущена суммаризация: "
            f"thread={thread_id}, chars={chars_total}, "
            f"threshold={summary_trigger_chars}"
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
        current_memory=(state.notes_md or "") if isinstance(state.notes_md, str) else "",
        user_messages_log=source_user_log,
        steps_offset=int(getattr(state, "dialog_steps_offset", 0) or 0),
    )
    raw_summary = str(summary_result.get("raw_summary") or "")
    memory_additions = summary_result.get("memory_additions", "")
    if logger is not None:
        logger.info("summary_received_raw_text=\n%s", raw_summary)
        logger.info(
            "summary_received_message_content=\n%s",
            summary_result.get("summary_message_content", ""),
        )
    if is_cli_mode:
        print("[summary] Полученный raw summary:")
        print(raw_summary)

    apply_summary_state_patch(
        state=state,
        summary_message_content=summary_result.get("summary_message_content", ""),
        memory_additions=memory_additions,
        summarized_messages_count=len(full_messages),
    )

    summary_payload = {
        "summary_message_content": summary_result.get("summary_message_content", ""),
        "notes_md": (state.notes_md or "") if isinstance(state.notes_md, str) else "",
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

    new_chars = calc_messages_chars_total(without_system_messages(getattr(state, "messages", [])))
    ratio = round((new_chars / chars_total), 4) if chars_total > 0 else 0.0
    if logger is not None:
        logger.info(
            "summary_completed reason=%s thread_id=%s old_chars=%s new_chars=%s compression_ratio=%s num_batches=%s memory_additions_chars=%s",
            trigger_reason,
            thread_id,
            chars_total,
            new_chars,
            ratio,
            summary_result.get("num_batches"),
            len(memory_additions or ""),
        )
    if is_cli_mode:
        print(
            "[summary] Завершена: "
            f"old_chars={chars_total}, new_chars={new_chars}, ratio={ratio}, "
            f"batches={summary_result.get('num_batches')}, "
            f"memory_additions_chars={len(memory_additions or '')}"
        )
    return True
