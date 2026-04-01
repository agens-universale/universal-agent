from typing import Any, Callable, Dict, List, Optional

from user_message_policy import TECHNICAL_TYPE_DIALOG_SUMMARY, wrap_technical_user_content
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func

from .summary_serialization import calc_messages_chars_total
from .thread_summarizer import summarize_dialog_via_chat_completions


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
    summary_message_content: str,
    memory_additions: str,
    summarized_messages_count: int,
) -> None:
    """
    Атомарно вносит в объект ``state`` результат compaction-суммаризации (без I/O и без решения «когда»).

    Зачем: каноничное состояние исполнения живёт в ``state`` (см. принципы слоёв); эта функция — точка,
    где согласованно обновляются заметки, оффсет шагов, привязка к предыдущему ответу API и список
    сообщений. Вызывается из ``maybe_run_summary_compaction`` после успешной суммаризации.

    Что делает:
        1. ``state.notes_md`` — дополняет текстом ``memory_additions``, только если он непустой;
           иначе поле не меняет.
        2. ``state.dialog_steps_offset`` — увеличивает на ``summarized_messages_count`` (служебно через
           ``max(0, int(...))``).
        3. ``state.previous_response_id`` — сбрасывает в ``None`` (контракт после compaction).
        4. ``state.messages`` — заменяет на компактный трейс: если в прежней истории есть ``system``,
           оставляет первое такое сообщение первым, затем одно техническое user-сообщение dialog summary
           (``wrap_technical_user_content`` + ``TECHNICAL_TYPE_DIALOG_SUMMARY``).

    Аргументы:
        state:
            Изменяемый in-place объект исполнения; ожидаются атрибуты ``notes_md``, ``dialog_steps_offset``,
            ``previous_response_id``, ``messages``.
        summary_message_content:
            Текст саммари предыдущего диалога; попадает внутрь служебного user-сообщения.
        memory_additions:
            Извлечённые durable дополнения к памяти треда (markdown); при непустом значении дописываются в notes.
        summarized_messages_count:
            Число сообщений, схлопнутых в этом проходе; участвует в ``dialog_steps_offset``.

    Возвращает:
        ``None``; побочный эффект — мутация ``state``.
    """
    notes_base = (state.notes_md or "") if isinstance(getattr(state, "notes_md", None), str) else ""
    notes_new = notes_base
    additions_text = str(memory_additions or "").strip()
    if additions_text:
        notes_new = (
            (notes_base + "\n\n" + additions_text).strip()
            if notes_base
            else additions_text
        )
        state.notes_md = notes_new
    prev_offset = int(getattr(state, "dialog_steps_offset", 0) or 0)
    state.dialog_steps_offset = prev_offset + max(0, int(summarized_messages_count or 0))
    state.previous_response_id = None

    summary_message = {
        "role": "user",
        "content": wrap_technical_user_content(
            (
                "Summarized context from previous dialogue.\n"
                f"{str(summary_message_content or '').strip()}"
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
    state.messages = [summary_message]
    if isinstance(system_message, dict):
        state.messages = [system_message, summary_message]
    return None


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
      не выполняя I/O внутри слоя compaction.

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
