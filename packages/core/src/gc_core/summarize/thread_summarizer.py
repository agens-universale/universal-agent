from typing import Any, Dict, List

from gc_core.config import _DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
from gc_core.config_modules.strip_tags_projection import (
    projection_tags_from_config,
)
from gc_core.config_modules.summary import SummaryConfig
from gc_core.execution.resources import (
    PromptResourceRequest,
    aload_prompt_resource,
)
from gc_core.intent_log import IntentRecord, serialize_for_meta
from gc_core.message_format import dialog_history as dialog_history
from gc_core.settings import settings_of
from gc_core.summarize import (
    memory_suggestions,
    prompt_loading,
    summary_llm_steps,
    summary_message_body,
)
from gc_core.summarize.llm import arequest_summary_text
from gc_core.summarize.summary_logging import (
    log_memory_patch_skipped_multi_batch,
    log_memory_patch_wm_unchanged,
)
from gc_core.summarize.summary_validation import (
    FALLBACK_SUMMARY,
    merge_repaired_summary,
    missing_section_tags,
    resolve_after_repair_failure,
    validate_summary_text,
)
from gc_core.summarize.working_memory_gateway import (
    WorkingMemoryDeltas,
    parse_memory_deltas,
    resolve_persistent_state_block,
    should_attempt_memory_deltas_repair,
)
from gc_core.utils.llm_chat_client import shorten_for_log


class _NullLogger:
    """Заглушка на месте отсутствующего ctx.logger: те же методы, ничего не пишет."""

    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None


_NULL_LOGGER = _NullLogger()


def _int_param(raw: Any, default: int) -> int:
    """int из настройки summary; нечисло или <= 0 → default."""
    try:
        value = int(raw if raw is not None else default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class _SummaryLimits:
    """Лимиты суммаризации из ``settings.summary`` с дефолтами."""

    def __init__(self, summary: SummaryConfig) -> None:
        self.last_steps_count = _int_param(summary.last_dialog_steps_count, 5)
        self.last_step_message_max_chars = _int_param(
            summary.last_dialog_step_message_max_chars,
            _DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS,
        )
        self.max_message_chars = _int_param(summary.max_message_chars, 50000)
        self.max_dialogue_chars = _int_param(
            summary.max_dialogue_chars, 1000000
        )
        self.max_accumulated_summary_chars = _int_param(
            summary.max_accumulated_summary_chars, 20000
        )
        # Размер батча для инкрементальной суммаризации.
        # По умолчанию нормализуется в config.py как int(trigger * 1.4).
        self.batch_chars_limit = _int_param(
            summary.incremental_batch_chars, 10**12
        )


def _empty_history_result(
    trace: Dict[str, Any], logger: Any
) -> Dict[str, Any]:
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


def _batch_user_prompt(
    batch_index: int,
    summarizer_prefix: str,
    batch_history_text: str,
    accumulated_summary: str,
) -> str:
    if batch_index == 1:
        return (
            f"{summarizer_prefix}"
            "<dialog_history>\n"
            f"{batch_history_text}\n"
            "</dialog_history>\n\n"
            "Produce the structured <summary> now."
        )
    return (
        f"{summarizer_prefix}"
        "<previous_summary>\n"
        f"{accumulated_summary}\n"
        "</previous_summary>\n\n"
        "<dialog_history_batch>\n"
        f"{batch_history_text}\n"
        "</dialog_history_batch>\n\n"
        "Update <previous_summary> using this batch. "
        "Output a full <summary> with all required child tags."
    )


class _BatchRun:
    """Один прогон инкрементальной суммаризации: батч → LLM → валидация/починка → сжатие."""

    def __init__(
        self,
        *,
        ctx: Any,
        state: Any,
        logger: Any,
        system_prompt: str,
        summarizer_prefix: str,
        max_message_chars: int,
        max_accumulated_summary_chars: int,
        max_tokens: int | None,
        batch_total: int,
        tags: Any = (),
        strip_think: bool = True,
    ) -> None:
        self.ctx = ctx
        self.state = state
        self.logger = logger
        self.log = logger if logger is not None else _NULL_LOGGER
        self.system_prompt = system_prompt
        self.summarizer_prefix = summarizer_prefix
        self.max_message_chars = max_message_chars
        self.max_accumulated_summary_chars = max_accumulated_summary_chars
        self.max_tokens = max_tokens
        self.batch_total = batch_total
        self.tags = tags
        self.strip_think = strip_think

    async def arun_batch(
        self,
        batch_index: int,
        batch_turns: List[List[Dict[str, Any]]],
        accumulated_summary: str,
    ) -> tuple:
        """→ (новая накопленная сводка, trace_batch)."""
        batch: List[Dict[str, Any]] = [m for turn in batch_turns for m in turn]
        batch_history_text = dialog_history.build_history_text(
            batch,
            max_chars=0,
            max_message_chars=self.max_message_chars,
            tags=self.tags,
            strip_think=self.strip_think,
        )
        self.log.info(
            "summary_batch_start batch=%s/%s batch_messages=%s batch_chars=%s",
            batch_index,
            self.batch_total,
            len(batch),
            len(batch_history_text),
        )
        user_prompt = _batch_user_prompt(
            batch_index,
            self.summarizer_prefix,
            batch_history_text,
            accumulated_summary,
        )
        request_messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        trace_batch: Dict[str, Any] = {
            "batch_index": batch_index,
            "batch_total": self.batch_total,
            "input_messages": request_messages,
            "input_system_prompt": self.system_prompt,
            "input_user_prompt": user_prompt,
            "output_summary_text": "",
        }
        self.log.info(
            "summary_batch_request_full_messages batch=%s/%s messages_full=\n%s",
            batch_index,
            self.batch_total,
            request_messages,
        )
        text = await arequest_summary_text(
            ctx=self.ctx,
            state=self.state,
            messages=request_messages,
            request_label=f"summary_batch_{batch_index}",
            consumer="summary",
            max_tokens=self.max_tokens,
            logger=self.logger,
        )
        trace_batch["output_summary_text"] = text
        self.log.info(
            "summary_batch_result batch=%s/%s summary_text=\n%s",
            batch_index,
            self.batch_total,
            text,
        )
        is_valid, reason = validate_summary_text(text)
        if not is_valid:
            text = await self._arepair(
                batch_index,
                text,
                reason,
                batch_history_text,
                accumulated_summary,
                trace_batch,
            )
        accumulated_summary = text
        if len(accumulated_summary) > self.max_accumulated_summary_chars:
            self.log.info(
                "summary_batch_compress batch=%s/%s before_chars=%s max_chars=%s",
                batch_index,
                self.batch_total,
                len(accumulated_summary),
                self.max_accumulated_summary_chars,
            )
            accumulated_summary = (
                await summary_llm_steps.acompress_summary_text(
                    ctx=self.ctx,
                    state=self.state,
                    summary_text=accumulated_summary,
                    max_chars=self.max_accumulated_summary_chars,
                    max_tokens=self.max_tokens,
                )
            )
        return accumulated_summary, trace_batch

    async def _arepair(
        self,
        batch_index: int,
        text: str,
        reason: str,
        batch_history_text: str,
        accumulated_summary: str,
        trace_batch: Dict[str, Any],
    ) -> str:
        self.log.warning(
            "summary_batch_invalid batch=%s/%s reason=%s text_chars=%s text_preview=%s",
            batch_index,
            self.batch_total,
            reason,
            len(text or ""),
            shorten_for_log(text or "", max_chars=500),
        )
        self.log.info(
            "summary_batch_repair_start batch=%s/%s reason=%s history_chars=%s user_requests_chars=%s invalid_summary_chars=%s",
            batch_index,
            self.batch_total,
            reason,
            len(batch_history_text),
            len(self.summarizer_prefix),
            len(text or ""),
        )
        missing_tags = missing_section_tags(text)
        repaired = await summary_llm_steps.arepair_summary_text(
            ctx=self.ctx,
            state=self.state,
            history_text=batch_history_text,
            persistent_state_prefix=self.summarizer_prefix,
            previous_summary=accumulated_summary if batch_index > 1 else "",
            invalid_summary=text,
            reason=reason,
            missing_tags=missing_tags,
            max_tokens=self.max_tokens,
        )
        merged = merge_repaired_summary(
            invalid_text=text, repaired_text=repaired
        )
        merged_valid, _ = validate_summary_text(merged)
        repaired_valid, _ = validate_summary_text(repaired)
        if merged_valid:
            final_text, repair_resolution = merged, "merged_valid"
        elif repaired_valid:
            final_text, repair_resolution = repaired, "repaired_valid"
        else:
            final_text, repair_resolution = resolve_after_repair_failure(
                invalid_text=text,
                merged_text=merged,
                repaired_text=repaired,
                accumulated_summary=accumulated_summary,
            )
        trace_batch["repair"] = {
            "reason": reason,
            "missing_tags": list(missing_tags),
            "repaired_text": repaired,
            "merged_text": merged,
            "repaired_valid": repaired_valid,
            "merged_valid": merged_valid,
            "resolution": repair_resolution,
            "final_text_after_repair": final_text,
        }
        self.log.info(
            "summary_batch_repair_result batch=%s/%s merged_valid=%s repaired_chars=%s merged_chars=%s final_chars=%s final_preview=%s",
            batch_index,
            self.batch_total,
            merged_valid,
            len(repaired or ""),
            len(merged or ""),
            len(final_text or ""),
            shorten_for_log(final_text or "", max_chars=500),
        )
        return final_text


async def _arun_patch_step(
    ctx: Any, state: Any, llm_result: Any, max_tokens: int | None
) -> tuple:
    """Разбор дельт WM с одной попыткой починки формата.

    Шаг в шаг повторяет ``working_memory_gateway.parse_memory_deltas_with_repair``
    (те же ветки, тот же порядок, тот же кортеж). Позвать его самого
    нельзя: он ждёт синхронный ``repair_fn``, а починка теперь корутина —
    синхронный колбэк на loop её не дождётся.
    """
    raw_output = llm_result.raw_response
    deltas, touched = parse_memory_deltas(raw_output, full_replace=True)
    repair_attempted = False
    raw_used = str(raw_output or "")
    if should_attempt_memory_deltas_repair(
        raw_output,
        deltas,
        min_chars_for_repair=100,
    ):
        # Починка формата дельт вторым вызовом LLM. Асинхронна, потому что зовёт LLM.
        repaired_raw = (
            await memory_suggestions.arequest_memory_deltas_format_repair(
                ctx=ctx,
                state=state,
                raw_output=str(raw_output or ""),
                max_tokens=max_tokens,
                full_replace=True,
            )
        )
        raw_used = str(repaired_raw or "")
        deltas, touched = parse_memory_deltas(raw_used, full_replace=True)
        repair_attempted = True
    repair_success = repair_attempted and (
        bool(touched) or not deltas.is_empty()
    )
    return deltas, touched, repair_attempted, repair_success, raw_used


def _skip_memory_patch_multi_batch(
    trace: Dict[str, Any],
    *,
    text: str,
    full_history_text: str,
    persistent_state_block: str,
    batches: int,
    logger: Any,
) -> None:
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
        log_memory_patch_skipped_multi_batch(logger, batches=batches)


async def _arun_single_batch_memory_patch(
    *,
    ctx: Any,
    state: Any,
    text: str,
    full_history_text: str,
    persistent_state_block: str,
    max_tokens: int | None,
    trace: Dict[str, Any],
    logger: Any,
) -> tuple:
    """Полная перезапись регистров WM (только single-batch) → (replace_deltas, replace_touched)."""
    replace_result = await memory_suggestions.arequest_patch_replace(
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
    ) = await _arun_patch_step(ctx, state, replace_result, max_tokens)
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
    if (
        logger is not None
        and not replace_touched
        and memory_replace_deltas.is_empty()
    ):
        log_memory_patch_wm_unchanged(
            logger,
            batches=1,
            format_repair_attempted=replace_repair,
            format_repair_success=replace_repair_ok,
            raw_response_chars=len(str(replace_result.raw_response or "")),
        )
    return memory_replace_deltas, replace_touched


async def asummarize_dialog_via_chat_completions(
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

    Основная (и единственная) реализация оркестрации; синхронный
    Все LLM-шаги ждутся нативно, поэтому компакция больше не держит поток
    из пула на всё время суммаризации.

    Назначение: основной оркестратор слоя суммаризации — подготовить текст для
    сообщения-резюме треда, опционально получить дельты working_memory через LLM,
    вернуть размеченный контент и подробный `trace` для наблюдаемости.

    Поведение (кратко):
        - Читает параметры из `settings.summary` (лимиты символов,
          размер батча, путь к промпту, число последних шагов,
          `last_dialog_step_message_max_chars` для хвоста `<last_dialog_steps>` и т.д.).
        - Усекает историю по `max_dialogue_chars`, нормализует и усекает лог
          пользовательских запросов.
        - Группирует сообщения в ходы и делит на батчи по `incremental_batch_chars`.
        - Для каждого батча вызывает LLM: первый батч — «с нуля», следующие —
          обновление `previous_summary`; валидирует ответ, при провале —
          `summary_llm_steps.arepair_summary_text` (без intent-блоков; multi-batch repair с `<previous_summary>`),
          иначе partial merge / предыдущий батч / `FALLBACK_SUMMARY`.
        - По необходимости сжимает накопленную сводку через `summary_llm_steps.acompress_summary_text`.
        - Шаг `memory_suggestions.arequest_patch_replace` (полная перезапись всех регистров WM)
          выполняется только если весь диалог уложился в один батч (мультибатч:
          `memory_patch.executed = false`, reason `skipped_multi_batch`).
        - Форматирует `last_dialog_steps` через `dialog_history.format_last_dialog_steps`.
          Полное тело сообщения для чата собирает `apply_summary_state_patch` после apply дельт в
          `state.shared.working_memory` (см. `compaction.run_summary_compaction`).

    Args:
        ctx: Контекст с настройками и `logger`.
        state: Состояние сессии (модель, заголовки бэкенда).
        full_history_messages: Полная история сообщений треда.
        persistent_state_block: Готовый блок ``<persistent_state>`` для prompt'ов (opaque).
            Если ``None`` — gateway соберёт snapshot из ``state.shared.working_memory``.
        user_intent_log: Закрытые user turn'ы для meta/trace и persisted summary (не в LLM prompt summarizer).
        current_user_intent: Открытый user turn — то же; intent в ``dialog_summary`` собирает ``compaction``.
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
    tags = projection_tags_from_config(cfg)
    # История для summarizer / memory patch всегда без ``<think>`` в прозе.
    # Persist ``reasoning_content`` — только канон ``state.messages``.
    strip_think = True
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
        return _empty_history_result(trace, logger)
    summary = settings_of(ctx).summary
    lim = _SummaryLimits(summary)
    last_steps_count = lim.last_steps_count
    last_step_message_max_chars = lim.last_step_message_max_chars
    max_message_chars = lim.max_message_chars
    max_dialogue_chars = lim.max_dialogue_chars
    max_accumulated_summary_chars = lim.max_accumulated_summary_chars
    batch_chars_limit = lim.batch_chars_limit
    prompt_path = summary.chat_completions_summary_prompt_path
    system_prompt = await aload_prompt_resource(
        ctx,
        PromptResourceRequest("summary", path=prompt_path),
        local=lambda: prompt_loading.load_prompt_template(
            str(prompt_path or prompt_loading.default_prompt_path())
        ),
    )

    history_for_summary = dialog_history.trim_history_to_max_dialogue_chars(
        full_history_messages,
        max_dialogue_chars=max_dialogue_chars,
        max_message_chars=max_message_chars,
        tags=tags,
        strip_think=strip_think,
    )
    # Полный текст усечённой истории (для memory patch step при single-batch).
    full_history_text = dialog_history.build_history_text(
        history_for_summary,
        max_chars=0,
        max_message_chars=max_message_chars,
        tags=tags,
        strip_think=strip_think,
    )
    closed_records = list(user_intent_log or [])
    open_records = list(current_user_intent or [])
    persistent_state_block = resolve_persistent_state_block(
        state, persistent_state_block
    )
    summarizer_prefix = (
        f"{persistent_state_block.strip()}\n\n"
        if persistent_state_block.strip()
        else ""
    )
    batches = dialog_history.split_turns_into_char_batches(
        dialog_history.group_into_turns(history_for_summary),
        batch_chars_limit=batch_chars_limit,
        max_message_chars=max_message_chars,
        tags=tags,
        strip_think=strip_think,
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
    run = _BatchRun(
        ctx=ctx,
        state=state,
        logger=logger,
        system_prompt=system_prompt,
        summarizer_prefix=summarizer_prefix,
        max_message_chars=max_message_chars,
        max_accumulated_summary_chars=max_accumulated_summary_chars,
        max_tokens=max_tokens,
        batch_total=len(batches),
        tags=tags,
        strip_think=strip_think,
    )
    accumulated_summary = ""
    for batch_index, batch_turns in enumerate(batches, start=1):
        accumulated_summary, trace_batch = await run.arun_batch(
            batch_index, batch_turns, accumulated_summary
        )
        trace["step1_batches"].append(trace_batch)
    text = accumulated_summary or FALLBACK_SUMMARY

    memory_append_deltas = WorkingMemoryDeltas()
    memory_replace_deltas = WorkingMemoryDeltas()
    append_touched: frozenset[str] = frozenset()
    replace_touched: frozenset[str] = frozenset()

    if len(batches) <= 1:
        (
            memory_replace_deltas,
            replace_touched,
        ) = await _arun_single_batch_memory_patch(
            ctx=ctx,
            state=state,
            text=text,
            full_history_text=full_history_text,
            persistent_state_block=persistent_state_block,
            max_tokens=max_tokens,
            trace=trace,
            logger=logger,
        )
    else:
        _skip_memory_patch_multi_batch(
            trace,
            text=text,
            full_history_text=full_history_text,
            persistent_state_block=persistent_state_block,
            batches=len(batches),
            logger=logger,
        )
    if logger is not None:
        logger.info(
            "summary_finish batches=%s memory_patch_executed=%s raw_summary_chars=%s memory_delta_registers=%s",
            len(batches),
            len(batches) <= 1,
            len(text),
            len(memory_append_deltas.to_dict())
            + len(memory_replace_deltas.to_dict()),
        )
        logger.info("summary_final_raw_text=\n%s", text)

    last_dialog_steps = dialog_history.format_last_dialog_steps(
        history_for_summary,
        max_steps=last_steps_count,
        message_max_chars=min(last_step_message_max_chars, max_message_chars),
        steps_offset=max(0, int(steps_offset or 0)),
        tags=tags,
        strip_think=strip_think,
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
