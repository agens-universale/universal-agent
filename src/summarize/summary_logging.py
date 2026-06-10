"""Логирование compaction и summary: процессный лог, CLI и summary_trace.jsonl."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

_TRIGGER_REASON_LABELS: dict[str, str] = {
    "turn_end": "конец хода после turn_finish",
    "pre_run": "старт сессии",
    "pre_backend": "середина хода (окно переполнено)",
    "subagent_pre_backend": "subagent, середина хода",
    "session_compact": "по запросу агента (session_compact)",
}


def trigger_reason_label(trigger_reason: str) -> str:
    reason = str(trigger_reason or "").strip()
    return _TRIGGER_REASON_LABELS.get(reason, reason or "неизвестно")


def logger_from_ctx(ctx: Any) -> Any | None:
    return getattr(ctx, "logger", None)


def resolve_summary_trace_path(ctx: Any) -> str:
    """
    Путь к JSONL trace суммаризации для текущего run.

    ``ctx.summary_trace_path`` задаётся в ``create_run_logger``; иначе fallback при ``save_logs``.
    """
    path = getattr(ctx, "summary_trace_path", "")
    if isinstance(path, str) and path.strip():
        return path.strip()
    cfg = getattr(ctx, "config_dict", {}) or {}
    if not cfg.get("save_logs", False):
        return ""
    base_dir = cfg.get("logs_dump_dir") or os.getcwd()
    return os.path.join(base_dir, "summary_trace.jsonl")


def append_summary_trace_record(
    ctx: Any,
    *,
    trigger_reason: str,
    trace_data: Mapping[str, Any] | None,
    newly_summarized_message_id: str,
) -> None:
    """Дописать одну строку в ``summary_trace*.jsonl`` (структурированный trace пайплайна)."""
    trace_path = resolve_summary_trace_path(ctx)
    if not trace_path:
        return
    trace_dir = os.path.dirname(trace_path)
    if trace_dir:
        os.makedirs(trace_dir, exist_ok=True)
    payload = {
        "trigger_reason": trigger_reason,
        "summary_trace": dict(trace_data or {}),
        "newly_summarized_message_id": newly_summarized_message_id,
    }
    with open(trace_path, "a", encoding="utf-8") as fd:
        fd.write(json.dumps(payload, ensure_ascii=False))
        fd.write("\n")


@dataclass(frozen=True)
class SummaryCompactionLog:
    """Контекст одного прохода compaction для единообразных логов."""

    trigger_reason: str
    thread_id: Any = None
    trigger_volume: int = 0
    trigger_threshold_chars: int | None = None
    is_cli_mode: bool = False

    @property
    def rule_label(self) -> str:
        return trigger_reason_label(self.trigger_reason)


def _cli_print(*lines: str) -> None:
    for line in lines:
        if line.startswith("[summary]"):
            print(line)
        else:
            print(f"[summary] {line}")


def log_compaction_started(
    logger: Any | None,
    log_ctx: SummaryCompactionLog,
    *,
    serialized_chars: int,
    history_messages_count: int,
) -> None:
    if logger is not None:
        logger.info(
            "summary_triggered reason=%s thread_id=%s trigger_volume=%s "
            "serialized_chars=%s history_messages=%s",
            log_ctx.trigger_reason,
            log_ctx.thread_id,
            log_ctx.trigger_volume,
            serialized_chars,
            history_messages_count,
        )
        logger.info(
            "summary_boundary phase=started rule=%s rule_id=%s thread_id=%s volume=%s%s",
            log_ctx.rule_label,
            log_ctx.trigger_reason,
            log_ctx.thread_id,
            log_ctx.trigger_volume,
            _threshold_log_suffix(log_ctx.trigger_threshold_chars),
        )
    if not log_ctx.is_cli_mode:
        return
    _cli_print(
        f"Сжатие: {log_ctx.rule_label} (правило={log_ctx.trigger_reason})",
        _started_cli_detail(log_ctx),
    )


def log_compaction_precheck(
    logger: Any | None,
    *,
    trigger_reason: str,
    thread_id: Any,
    history_messages_count: int,
    trigger_volume: int,
    serialized_chars: int,
    threshold: int,
) -> None:
    if logger is None:
        return
    logger.info(
        "summary_precheck reason=%s thread_id=%s history_messages=%s "
        "trigger_volume=%s serialized_chars=%s threshold=%s",
        trigger_reason,
        thread_id,
        history_messages_count,
        trigger_volume,
        serialized_chars,
        threshold,
    )


def log_summary_raw_text(
    logger: Any | None,
    log_ctx: SummaryCompactionLog,
    raw_summary: str,
) -> None:
    text = str(raw_summary or "")
    if logger is not None:
        logger.info(
            "summary_received_raw_text rule_id=%s thread_id=%s\n%s",
            log_ctx.trigger_reason,
            log_ctx.thread_id,
            text,
        )
    if log_ctx.is_cli_mode:
        _cli_print("Полный текст сводки:")
        print(text)


def log_summary_message_body(
    logger: Any | None,
    log_ctx: SummaryCompactionLog,
    message_content: str,
) -> None:
    text = str(message_content or "")
    if logger is not None:
        logger.info(
            "summary_message_body rule_id=%s thread_id=%s\n%s",
            log_ctx.trigger_reason,
            log_ctx.thread_id,
            text,
        )
    if log_ctx.is_cli_mode:
        _cli_print("Тело сообщения dialog_summary:")
        print(text)


def log_compaction_completed(
    logger: Any | None,
    log_ctx: SummaryCompactionLog,
    *,
    serialized_before: int,
    serialized_after: int,
    compression_ratio: float,
    num_batches: Any,
    memory_delta_chars: int,
    archive_suffix: str | None,
) -> None:
    archive = _normalize_archive_suffix(archive_suffix)
    if logger is not None:
        logger.info(
            "summary_completed reason=%s thread_id=%s trigger_volume=%s "
            "serialized_before=%s serialized_after=%s compression_ratio=%s "
            "num_batches=%s memory_delta_chars=%s archive_suffix=%s",
            log_ctx.trigger_reason,
            log_ctx.thread_id,
            log_ctx.trigger_volume,
            serialized_before,
            serialized_after,
            compression_ratio,
            num_batches,
            memory_delta_chars,
            archive,
        )
        logger.info(
            "summary_boundary phase=completed rule=%s rule_id=%s thread_id=%s "
            "volume=%s%s%s",
            log_ctx.rule_label,
            log_ctx.trigger_reason,
            log_ctx.thread_id,
            log_ctx.trigger_volume,
            _threshold_log_suffix(log_ctx.trigger_threshold_chars),
            _archive_log_suffix(archive),
        )
        logger.info(
            "summary_boundary_stats serialized_before=%s serialized_after=%s compression_ratio=%s",
            serialized_before,
            serialized_after,
            compression_ratio,
        )
    if not log_ctx.is_cli_mode:
        return
    _cli_print(f"Сжатие завершено: {log_ctx.rule_label}")
    _cli_print(
        f"  было={serialized_before} стало={serialized_after} "
        f"коэффициент={compression_ratio}"
    )
    if archive:
        _cli_print(f"  архив треда: __pre_summary__{archive}")


def log_compaction_failed(
    logger: Any | None,
    log_ctx: SummaryCompactionLog,
    error: BaseException,
    *,
    exc_info: bool = True,
) -> None:
    if logger is not None:
        logger.warning(
            "summary_failed reason=%s rule=%s thread_id=%s error=%s",
            log_ctx.trigger_reason,
            log_ctx.rule_label,
            log_ctx.thread_id,
            error,
            exc_info=exc_info,
        )
    if log_ctx.is_cli_mode:
        _cli_print(
            f"Ошибка суммаризации ({log_ctx.rule_label}): error={error}"
        )


def log_summary_slice_fallback(
    logger: Any | None,
    log_ctx: SummaryCompactionLog,
    *,
    slice_mode: str,
    raw_summary_chars: int,
) -> None:
    if logger is None:
        return
    logger.warning(
        "summary_slice_fallback rule_id=%s thread_id=%s slice_mode=%s "
        "raw_summary_chars=%s agent_gets_full_raw=true",
        log_ctx.trigger_reason,
        log_ctx.thread_id,
        slice_mode,
        raw_summary_chars,
    )


def log_memory_patch_wm_unchanged(
    logger: Any | None,
    *,
    batches: int,
    format_repair_attempted: bool,
    format_repair_success: bool,
    raw_response_chars: int,
) -> None:
    if logger is None:
        return
    logger.warning(
        "summary_memory_patch_wm_unchanged batches=%s "
        "format_repair_attempted=%s format_repair_success=%s raw_response_chars=%s "
        "agent_slice_may_still_be_short=true",
        batches,
        format_repair_attempted,
        format_repair_success,
        raw_response_chars,
    )


def log_memory_patch_skipped_multi_batch(
    logger: Any | None,
    *,
    batches: int,
) -> None:
    if logger is None:
        return
    logger.info(
        "summary_memory_patch_skipped_multi_batch batches=%s "
        "working_memory_not_updated_from_summary=true",
        batches,
    )


def log_pre_run_max_rounds(
    logger: Any | None,
    *,
    thread_id: Any,
    max_rounds: int,
) -> None:
    if logger is None:
        return
    logger.warning(
        "summary_pre_run_max_rounds thread_id=%s rounds=%s",
        thread_id,
        max_rounds,
    )


def _threshold_log_suffix(trigger_threshold_chars: int | None) -> str:
    if trigger_threshold_chars is not None and trigger_threshold_chars > 0:
        return f" threshold={trigger_threshold_chars}"
    return ""


def _archive_log_suffix(archive_suffix: str | None) -> str:
    archive = _normalize_archive_suffix(archive_suffix)
    if archive:
        return f" archive=__pre_summary__{archive}"
    return ""


def _normalize_archive_suffix(archive_suffix: str | None) -> str | None:
    if isinstance(archive_suffix, str):
        stripped = archive_suffix.strip()
        return stripped or None
    return None


def _started_cli_detail(log_ctx: SummaryCompactionLog) -> str:
    line = f"  thread={log_ctx.thread_id} объём={log_ctx.trigger_volume}"
    if log_ctx.trigger_threshold_chars is not None and log_ctx.trigger_threshold_chars > 0:
        line += f" порог={log_ctx.trigger_threshold_chars}"
    return line
