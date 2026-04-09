from typing import Any, Dict, List

from summarize import (
    dialog_history,
    llm_chat,
    memory_suggestions,
    prompt_loading,
    summary_llm_steps,
    summary_message_body,
)
from summarize.summary_validation import FALLBACK_SUMMARY, validate_summary_text
from user_message_policy import normalize_user_log_entries
from utils.project_data import get_project_data


def summarize_dialog_via_chat_completions(
    *,
    ctx: Any,
    state: Any,
    full_history_messages: List[Dict[str, Any]],
    current_memory: str = "",
    user_messages_log: List[str] | None = None,
    max_tokens: int = 2500,
    steps_offset: int = 0,
) -> Dict[str, Any]:
    """Строит структурированную сводку диалога через chat completions (в т.ч. батчами).

    Назначение: основной оркестратор слоя суммаризации — подготовить текст для
    сообщения-резюме треда, опционально предложить доп. память, вернуть размеченный
    контент и подробный `trace` для наблюдаемости.

    Поведение (кратко):
        - Читает параметры из `ctx.config_dict["summary_params"]` (лимиты символов,
          размер батча, путь к промпту, число последних шагов и т.д.).
        - Усекает историю по `max_dialogue_chars`, нормализует и усекает лог
          пользовательских запросов.
        - Группирует сообщения в ходы и делит на батчи по `incremental_batch_chars`.
        - Для каждого батча вызывает LLM: первый батч — «с нуля», следующие —
          обновление `previous_summary`; валидирует ответ, при провале —
          `summary_llm_steps.repair_summary_text`, иначе fallback на предыдущую сводку или `FALLBACK_SUMMARY`.
        - По необходимости сжимает накопленную сводку через `summary_llm_steps.compress_summary_text`.
        - Шаг `memory_suggestions.suggest_memory_additions` выполняется только если весь диалог уложился
          в один батч (мультибатч: в trace фиксируется пропуск).
        - Форматирует `last_dialog_steps` через `dialog_history.format_last_dialog_steps`.
          Полное тело сообщения для чата собирает `apply_summary_state_patch` после обновления
          `state.shared.memory_files` (см. `compaction.run_summary_compaction`).

    Args:
        ctx: Контекст с `config_dict` и `logger`.
        state: Состояние сессии (модель, заголовки бэкенда).
        full_history_messages: Полная история сообщений треда.
        current_memory: Актуальные заметки/память для вставки в итоговое сообщение
            и в шаг памяти.
        user_messages_log: Внешний журнал реплик пользователя (`None` допустим):
            нормализуется и усекается, затем входит в блок `<user_requests_log>`;
            при пустом результате в промпт подставляется маркер `- not found`.
        max_tokens: Лимит ответа LLM на батч и связанные вызовы (сжатие/починка).
        steps_offset: Сдвиг нумерации в `dialog_history.format_last_dialog_steps` (инкрементальные сценарии).

    Returns:
        Словарь с полями:
            ``raw_summary`` — текст сводки без блока предложенной памяти;
            ``summary_message_content`` — для непустой истории пустая строка; итоговое тело строит
                ``apply_summary_state_patch``. Для пустой истории — готовое тело без вызова compaction;
            ``user_messages_log`` — очищенный список пользовательских строк;
            ``memory_additions`` — извлечённый фрагмент для дописывания в заметки (сырой текст) или пусто;
            ``last_dialog_steps`` — форматированный хвост диалога;
            ``num_batches`` — число батчей LLM;
            ``trace`` — структура с шагами, промптами и результатами для отладки.
    """
    cfg = getattr(ctx, "config_dict", {}) or {}
    logger = getattr(ctx, "logger", None)
    trace: Dict[str, Any] = {
        "step1_batches": [],
        "step2_memory_update": {
            "executed": False,
            "input": {},
            "output": "",
        },
        "final_output": {},
    }
    if not full_history_messages:
        if logger is not None:
            logger.info("summary_empty_history: no messages to summarize")
        summary_content = summary_message_body.build_dialog_summary_content(
            summary_text="",
            last_dialog_steps="",
        )
        trace["final_output"] = {
            "raw_summary": "",
            "memory_additions": "",
            "summary_message_content": summary_content,
        }
        return {
            "raw_summary": "",
            "summary_message_content": summary_content,
            "user_messages_log": [],
            "memory_additions": "",
            "last_dialog_steps": "",
            "num_batches": 0,
            "trace": trace,
        }
    summary_params = cfg.get("summary_params") if isinstance(cfg.get("summary_params"), dict) else {}
    try:
        last_steps_count = int(summary_params.get("last_dialog_steps_count", 5))
    except (TypeError, ValueError):
        last_steps_count = 5
    try:
        last_step_message_max_chars = int(
            summary_params.get("last_dialog_step_message_max_chars", 5000)
        )
    except (TypeError, ValueError):
        last_step_message_max_chars = 5000
    if last_steps_count <= 0:
        last_steps_count = 5
    if last_step_message_max_chars <= 0:
        last_step_message_max_chars = 5000
    try:
        max_message_chars = int(summary_params.get("max_message_chars", 50000))
    except (TypeError, ValueError):
        max_message_chars = 50000
    try:
        max_dialogue_chars = int(summary_params.get("max_dialogue_chars", 1000000))
    except (TypeError, ValueError):
        max_dialogue_chars = 1000000
    if max_message_chars <= 0:
        max_message_chars = 50000
    if max_dialogue_chars <= 0:
        max_dialogue_chars = 1000000
    try:
        max_accumulated_summary_chars = int(
            summary_params.get("max_accumulated_summary_chars", 20000)
        )
    except (TypeError, ValueError):
        max_accumulated_summary_chars = 20000
    if max_accumulated_summary_chars <= 0:
        max_accumulated_summary_chars = 20000
    try:
        original_user_messages_max_chars = int(
            summary_params.get("original_user_messages_max_chars", 20000)
        )
    except (TypeError, ValueError):
        original_user_messages_max_chars = 20000
    if original_user_messages_max_chars <= 0:
        original_user_messages_max_chars = 20000
    # Размер батча для инкрементальной суммаризации.
    # По умолчанию нормализуется в config.py как int(trigger * 1.4).
    try:
        batch_chars_limit = int(
            summary_params.get("incremental_batch_chars", 0)
        )
    except (TypeError, ValueError):
        batch_chars_limit = 0
    if batch_chars_limit <= 0:
        batch_chars_limit = 10**12
    prompt_path = summary_params.get("chat_completions_summary_prompt_path") or prompt_loading.default_prompt_path()
    system_prompt = prompt_loading.load_prompt_template(str(prompt_path))

    history_for_summary = dialog_history.trim_history_to_max_dialogue_chars(
        full_history_messages,
        max_dialogue_chars=max_dialogue_chars,
        max_message_chars=max_message_chars,
    )
    # `state.user_messages_log` is expected to be already filtered and deduplicated by State
    # via user_message_policy. We still normalize defensively here because this function can be
    # called with persisted/raw data in tests and integrations.
    user_requests = normalize_user_log_entries(user_messages_log)
    user_requests = dialog_history.trim_original_user_messages_to_max_chars(
        user_requests, original_user_messages_max_chars
    )
    user_requests_text = (
        "\n".join(f"- {x}" for x in user_requests) if user_requests else "- not found"
    )
    batches = dialog_history.split_turns_into_char_batches(
        dialog_history.group_into_turns(history_for_summary),
        batch_chars_limit=batch_chars_limit,
        max_message_chars=max_message_chars,
    )
    if not batches:
        batches = [dialog_history.group_into_turns(history_for_summary)]
    if logger is not None:
        logger.info(
            "summary_start messages_total=%s messages_after_trim=%s batches=%s trigger_prompt=%s",
            len(full_history_messages),
            len(history_for_summary),
            len(batches),
            prompt_path,
        )
    pd = get_project_data()
    project_block = (
        "<project_data>\n"
        f"OS: {pd['OS']}\n"
        f"shell: {pd['shell']}\n"
        f"projectDir: {pd['projectDir']}\n"
        f"currentDate: {pd['currentDate']}\n"
        f"isGitProject: {pd['isGitProject']}\n"
        f"projectDirListing:\n{pd['projectDirListing']}\n"
        "</project_data>\n\n"
    )
    accumulated_summary = ""
    for batch_index, batch_turns in enumerate(batches, start=1):
        batch: List[Dict[str, Any]] = [m for turn in batch_turns for m in turn]
        history_text = dialog_history.build_history_text(
            batch,
            max_chars=0,
            max_message_chars=max_message_chars,
        )
        if logger is not None:
            logger.info(
                "summary_batch_start batch=%s/%s batch_messages=%s batch_chars=%s",
                batch_index,
                len(batches),
                len(batch),
                len(history_text),
            )
        if batch_index == 1:
            user_prompt = (
                "<user_requests_log>\n"
                f"{user_requests_text}\n"
                "</user_requests_log>\n\n"
                "<dialog_history>\n"
                f"{history_text}\n"
                "</dialog_history>\n\n"
                f"{project_block}"
                "Produce the structured summary now."
            )
        else:
            user_prompt = (
                "<user_requests_log>\n"
                f"{user_requests_text}\n"
                "</user_requests_log>\n\n"
                "<previous_summary>\n"
                f"{accumulated_summary}\n"
                "</previous_summary>\n\n"
                "<dialog_history_batch>\n"
                f"{history_text}\n"
                "</dialog_history_batch>\n\n"
                f"{project_block}"
                "Update previous_summary using this batch. "
                "Keep all important facts/decisions/constraints from both."
            )
        request_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        trace_batch: Dict[str, Any] = {
            "batch_index": batch_index,
            "batch_total": len(batches),
            "input_messages": request_messages,
            "input_system_prompt": system_prompt,
            "input_user_prompt": user_prompt,
            "output_summary_text": "",
        }
        if logger is not None:
            logger.info(
                "summary_batch_request_full_messages batch=%s/%s messages_full=\n%s",
                batch_index,
                len(batches),
                request_messages,
            )
        text = llm_chat.request_chat_completion_text(
            ctx=ctx,
            state=state,
            messages=request_messages,
            max_tokens=max_tokens,
            request_label=f"summary_batch_{batch_index}",
        )
        llm_chat.log_llm_prompt_and_result(
            logger=logger,
            label=f"summary_batch_{batch_index}",
            messages=request_messages,
            output_text=text,
        )
        trace_batch["output_summary_text"] = text
        if logger is not None:
            logger.info(
                "summary_batch_result batch=%s/%s summary_text=\n%s",
                batch_index,
                len(batches),
                text,
            )
        is_valid, reason = validate_summary_text(text)
        if not is_valid:
            if logger is not None:
                logger.warning(
                    "summary_batch_invalid batch=%s/%s reason=%s text_chars=%s text_preview=%s",
                    batch_index,
                    len(batches),
                    reason,
                    len(text or ""),
                    llm_chat.shorten_for_log(text or "", max_chars=500),
                )
                logger.info(
                    "summary_batch_repair_start batch=%s/%s reason=%s history_chars=%s user_requests_chars=%s invalid_summary_chars=%s",
                    batch_index,
                    len(batches),
                    reason,
                    len(history_text),
                    len(user_requests_text),
                    len(text or ""),
                )
            repaired = summary_llm_steps.repair_summary_text(
                ctx=ctx,
                state=state,
                history_text=history_text,
                user_requests_text=user_requests_text,
                invalid_summary=text,
                reason=reason,
                max_tokens=max_tokens,
                project_data_block=project_block,
            )
            repaired_valid, _ = validate_summary_text(repaired)
            text = repaired if repaired_valid else (accumulated_summary or FALLBACK_SUMMARY)
            trace_batch["repair"] = {
                "reason": reason,
                "repaired_text": repaired,
                "repaired_valid": repaired_valid,
                "final_text_after_repair": text,
            }
            if logger is not None:
                logger.info(
                    "summary_batch_repair_result batch=%s/%s repaired_valid=%s repaired_chars=%s final_chars=%s final_preview=%s",
                    batch_index,
                    len(batches),
                    repaired_valid,
                    len(repaired or ""),
                    len(text or ""),
                    llm_chat.shorten_for_log(text or "", max_chars=500),
                )
        accumulated_summary = text
        trace["step1_batches"].append(trace_batch)
        if len(accumulated_summary) > max_accumulated_summary_chars:
            if logger is not None:
                logger.info(
                    "summary_batch_compress batch=%s/%s before_chars=%s max_chars=%s",
                    batch_index,
                    len(batches),
                    len(accumulated_summary),
                    max_accumulated_summary_chars,
                )
            accumulated_summary = summary_llm_steps.compress_summary_text(
                ctx=ctx,
                state=state,
                summary_text=accumulated_summary,
                max_chars=max_accumulated_summary_chars,
                max_tokens=max_tokens,
            )
    text = accumulated_summary or FALLBACK_SUMMARY

    history_text = dialog_history.build_history_text(
        history_for_summary,
        max_chars=0,
        max_message_chars=max_message_chars,
    )
    # Второй такт апдейта памяти: просим LLM вернуть полную актуализированную
    # версию journal (не дельту). Для multi-batch пока пропускаем, чтобы не
    # удлинять критический путь суммаризации.
    memory_additions = ""
    if len(batches) <= 1:
        trace["step2_memory_update"] = {
            "executed": True,
            "input": {
                "history_text": history_text,
                "summary_text": text,
                "current_journal": current_memory,
            },
            "output": "",
            "llm": {},
        }
        memory_result = memory_suggestions.suggest_memory_additions(
            ctx=ctx,
            state=state,
            history_text=history_text,
            summary_text=text,
            current_memory=current_memory,
        )
        if isinstance(memory_result, str):
            memory_result = memory_suggestions.MemoryAdditionsResult(
                extracted=memory_result,
                request_messages=[],
                raw_response="",
            )
        memory_additions = memory_result.extracted
        trace["step2_memory_update"]["output"] = memory_additions
        trace["step2_memory_update"]["llm"] = {
            "request_messages": memory_result.request_messages,
            "raw_response": memory_result.raw_response,
            "extracted": memory_result.extracted,
        }
    else:
        trace["step2_memory_update"] = {
            "executed": False,
            "input": {
                "history_text": history_text,
                "summary_text": text,
                "current_memory": current_memory,
                "reason": "skipped_for_multi_batch",
            },
            "output": "",
        }
    if logger is not None:
        logger.info(
            "summary_finish batches=%s memory_step_executed=%s raw_summary_chars=%s memory_additions_chars=%s",
            len(batches),
            len(batches) <= 1,
            len(text),
            len(memory_additions or ""),
        )
        logger.info("summary_final_raw_text=\n%s", text)

    last_dialog_steps = dialog_history.format_last_dialog_steps(
        history_for_summary,
        max_steps=last_steps_count,
        message_max_chars=min(last_step_message_max_chars, max_message_chars),
        steps_offset=max(0, int(steps_offset or 0)),
    )
    trace["final_output"] = {
        "raw_summary": text,
        "memory_additions": memory_additions,
        "updated_journal": memory_additions,
    }
    return {
        "raw_summary": text,
        "summary_message_content": "",
        "user_messages_log": user_requests,
        "memory_additions": memory_additions,
        "updated_journal": memory_additions,
        "last_dialog_steps": last_dialog_steps,
        "num_batches": len(batches),
        "trace": trace,
    }

