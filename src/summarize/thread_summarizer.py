from typing import Any, Dict, List

from config import _DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
import message_format.dialog_history as dialog_history
from intent_log import IntentRecord, serialize_for_meta
from summarize import (
    memory_suggestions,
    prompt_loading,
    summary_llm_steps,
    summary_message_body,
)
from summarize.summary_message_body import (
    build_intent_blocks_prefix,
    resolve_intent_log_preview_chars,
)
from summarize.summary_validation import FALLBACK_SUMMARY, validate_summary_text
from summarize.llm import request_summary_text
from summarize.working_memory_gateway import (
    WorkingMemoryDeltas,
    parse_memory_deltas_with_repair,
    resolve_persistent_state_block,
)
from utils.llm_chat_client import log_llm_prompt_and_result, shorten_for_log
from utils.project_data import get_project_data


def summarize_dialog_via_chat_completions(
    *,
    ctx: Any,
    state: Any,
    full_history_messages: List[Dict[str, Any]],
    persistent_state_block: str | None = None,
    user_intent_log: List[IntentRecord] | None = None,
    current_user_intent: List[IntentRecord] | None = None,
    max_tokens: int | None = None,
    steps_offset: int = 0,
) -> Dict[str, Any]:
    """Строит структурированную сводку диалога через chat completions (в т.ч. батчами).

    Назначение: основной оркестратор слоя суммаризации — подготовить текст для
    сообщения-резюме треда, опционально получить дельты working_memory через LLM,
    вернуть размеченный контент и подробный `trace` для наблюдаемости.

    Поведение (кратко):
        - Читает параметры из `ctx.config_dict["summary_params"]` (лимиты символов,
          размер батча, путь к промпту, число последних шагов,
          `last_dialog_step_message_max_chars` для хвоста `<last_dialog_steps>` и т.д.).
        - Усекает историю по `max_dialogue_chars`, нормализует и усекает лог
          пользовательских запросов.
        - Группирует сообщения в ходы и делит на батчи по `incremental_batch_chars`.
        - Для каждого батча вызывает LLM: первый батч — «с нуля», следующие —
          обновление `previous_summary`; валидирует ответ, при провале —
          `summary_llm_steps.repair_summary_text`, иначе fallback на предыдущую сводку или `FALLBACK_SUMMARY`.
        - По необходимости сжимает накопленную сводку через `summary_llm_steps.compress_summary_text`.
        - Шаг `memory_suggestions.request_patch_replace` (полная перезапись всех регистров WM)
          выполняется только если весь диалог уложился в один батч (мультибатч:
          `memory_patch.executed = false`, reason `skipped_multi_batch`).
        - Форматирует `last_dialog_steps` через `dialog_history.format_last_dialog_steps`.
          Полное тело сообщения для чата собирает `apply_summary_state_patch` после apply дельт в
          `state.shared.working_memory` (см. `compaction.run_summary_compaction`).

    Args:
        ctx: Контекст с `config_dict` и `logger`.
        state: Состояние сессии (модель, заголовки бэкенда).
        full_history_messages: Полная история сообщений треда.
        persistent_state_block: Готовый блок ``<persistent_state>`` для prompt'ов (opaque).
            Если ``None`` — gateway соберёт snapshot из ``state.shared.working_memory``.
        user_intent_log: Закрытые user turn'ы (`None` допустим): нормализуется и усекается,
            затем входит в блок `<user_intent_log>`; при пустом — `- not found`.
        current_user_intent: Открытый user turn: блок `<current_user_intent>`; при пустом — `- not found`.
        max_tokens: Лимит ответа LLM на батч и связанные вызовы (сжатие/починка).
        steps_offset: Сдвиг нумерации в `dialog_history.format_last_dialog_steps` (инкрементальные сценарии).

    Returns:
        Словарь с полями:
            ``raw_summary`` — текст сводки без блока предложенной памяти;
            ``summary_message_content`` — для непустой истории пустая строка; итоговое тело строит
                ``apply_summary_state_patch``. Для пустой истории — готовое тело без вызова compaction;
            ``user_intent_log`` / ``current_user_intent`` — нормализованные снимки для persisted summary;
            ``memory_append_deltas`` / ``memory_replace_deltas`` — ``WorkingMemoryDeltas``
                (пустые DTO для multi-batch);
            ``last_dialog_steps`` — форматированный хвост диалога;
            ``num_batches`` — число батчей LLM;
            ``trace`` — структура с шагами, промптами и результатами для отладки.
    """
    cfg = getattr(ctx, "config_dict", {}) or {}
    logger = getattr(ctx, "logger", None)
    trace: Dict[str, Any] = {
        "step1_batches": [],
        "memory_patch": {
            "executed": False,
            "append": {},
            "replace": {},
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
            "memory_append_deltas": {},
            "memory_replace_deltas": {},
            "summary_message_content": summary_content,
        }
        empty_deltas = WorkingMemoryDeltas()
        return {
            "raw_summary": "",
            "summary_message_content": summary_content,
            "user_intent_log": [],
            "current_user_intent": [],
            "memory_append_deltas": empty_deltas,
            "memory_replace_deltas": empty_deltas,
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
            summary_params.get(
                "last_dialog_step_message_max_chars",
                _DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS,
            )
        )
    except (TypeError, ValueError):
        last_step_message_max_chars = _DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
    if last_steps_count <= 0:
        last_steps_count = 5
    if last_step_message_max_chars <= 0:
        last_step_message_max_chars = _DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
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
    # Полный текст усечённой истории (для memory patch step при single-batch).
    full_history_text = dialog_history.build_history_text(
        history_for_summary,
        max_chars=0,
        max_message_chars=max_message_chars,
    )
    closed_records = list(user_intent_log or [])
    open_records = list(current_user_intent or [])
    persistent_state_block = resolve_persistent_state_block(
        state, persistent_state_block
    )
    intent_blocks_prefix = build_intent_blocks_prefix(
        closed_records,
        open_records,
        intent_log_preview_chars=resolve_intent_log_preview_chars(summary_params),
    )
    if persistent_state_block.strip():
        intent_blocks_prefix = f"{intent_blocks_prefix}{persistent_state_block.strip()}\n\n"
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
    pd = get_project_data(cfg)
    project_block = (
        "<project_data>\n"
        f"OS: {pd['OS']}\n"
        f"shell: {pd['shell']}\n"
        f"projectDir: {pd['projectDir']}\n"
        f"projectDirWritable: {pd['projectDirWritable']}\n"
        f"allowed_write_paths:\n{pd['allowed_write_paths']}\n"
        f"currentDate: {pd['currentDate']}\n"
        f"isGitProject: {pd['isGitProject']}\n"
        f"projectDirListing:\n{pd['projectDirListing']}\n"
        "</project_data>\n\n"
    )
    accumulated_summary = ""
    for batch_index, batch_turns in enumerate(batches, start=1):
        batch: List[Dict[str, Any]] = [m for turn in batch_turns for m in turn]
        batch_history_text = dialog_history.build_history_text(
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
                len(batch_history_text),
            )
        if batch_index == 1:
            user_prompt = (
                f"{intent_blocks_prefix}"
                "<dialog_history>\n"
                f"{batch_history_text}\n"
                "</dialog_history>\n\n"
                f"{project_block}"
                "Produce the structured summary now."
            )
        else:
            user_prompt = (
                f"{intent_blocks_prefix}"
                "<previous_summary>\n"
                f"{accumulated_summary}\n"
                "</previous_summary>\n\n"
                "<dialog_history_batch>\n"
                f"{batch_history_text}\n"
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
        text = request_summary_text(
            ctx=ctx,
            state=state,
            messages=request_messages,
            request_label=f"summary_batch_{batch_index}",
            max_tokens=max_tokens,
        )
        log_llm_prompt_and_result(
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
                    shorten_for_log(text or "", max_chars=500),
                )
                logger.info(
                    "summary_batch_repair_start batch=%s/%s reason=%s history_chars=%s user_requests_chars=%s invalid_summary_chars=%s",
                    batch_index,
                    len(batches),
                    reason,
                    len(batch_history_text),
                    len(intent_blocks_prefix),
                    len(text or ""),
                )
            repaired = summary_llm_steps.repair_summary_text(
                ctx=ctx,
                state=state,
                history_text=batch_history_text,
                intent_blocks_prefix=intent_blocks_prefix,
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
                    shorten_for_log(text or "", max_chars=500),
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

    memory_append_deltas = WorkingMemoryDeltas()
    memory_replace_deltas = WorkingMemoryDeltas()
    append_touched: frozenset[str] = frozenset()
    replace_touched: frozenset[str] = frozenset()

    def _repair_fn(raw: str) -> str:
        return memory_suggestions.request_memory_deltas_format_repair(
            ctx=ctx,
            state=state,
            raw_output=raw,
            max_tokens=max_tokens,
            full_replace=True,
        )

    def _run_patch_step(
        llm_result: Any,
    ) -> tuple[WorkingMemoryDeltas, frozenset[str], bool, bool, str]:
        deltas, touched, repair_attempted, raw_used = parse_memory_deltas_with_repair(
            llm_result.raw_response,
            repair_fn=_repair_fn,
            min_chars_for_repair=100,
            full_replace=True,
        )
        repair_success = repair_attempted and (bool(touched) or not deltas.is_empty())
        return deltas, touched, repair_attempted, repair_success, raw_used

    if len(batches) <= 1:
        replace_result = memory_suggestions.request_patch_replace(
            ctx=ctx,
            state=state,
            history_text=full_history_text,
            summary_text=text,
            persistent_state_block=persistent_state_block,
        )
        (
            memory_replace_deltas,
            replace_touched,
            replace_repair,
            replace_repair_ok,
            replace_raw,
        ) = _run_patch_step(replace_result)
        trace["memory_patch"] = {
            "executed": True,
            "mode": "full_replace_all_registers",
            "input": {
                "history_text": full_history_text,
                "summary_text": text,
                "persistent_state": persistent_state_block,
            },
            "append": {
                "skipped": True,
                "reason": "summary_patch_uses_full_replace_only",
                "output": {},
            },
            "replace": {
                "output": memory_replace_deltas.to_dict(),
                "format_repair_attempted": replace_repair,
                "format_repair_success": replace_repair_ok,
                "llm": {
                    "request_messages": replace_result.request_messages,
                    "raw_response": replace_result.raw_response,
                    "raw_response_parsed": replace_raw,
                },
            },
        }
    else:
        trace["memory_patch"] = {
            "executed": False,
            "reason": "skipped_multi_batch",
            "input": {
                "history_text": full_history_text,
                "summary_text": text,
                "persistent_state": persistent_state_block,
            },
            "append": {"output": {}},
            "replace": {"output": {}},
        }
    if logger is not None:
        logger.info(
            "summary_finish batches=%s memory_patch_executed=%s raw_summary_chars=%s memory_delta_registers=%s",
            len(batches),
            len(batches) <= 1,
            len(text),
            len(memory_append_deltas.to_dict()) + len(memory_replace_deltas.to_dict()),
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
        "memory_append_deltas": memory_append_deltas.to_dict(),
        "memory_replace_deltas": memory_replace_deltas.to_dict(),
    }
    return {
        "raw_summary": text,
        "summary_message_content": "",
        "user_intent_log": serialize_for_meta(closed_records),
        "current_user_intent": serialize_for_meta(open_records),
        "memory_append_deltas": memory_append_deltas,
        "memory_replace_deltas": memory_replace_deltas,
        "memory_append_touched": sorted(append_touched),
        "memory_replace_touched": sorted(replace_touched),
        "last_dialog_steps": last_dialog_steps,
        "num_batches": len(batches),
        "trace": trace,
    }

