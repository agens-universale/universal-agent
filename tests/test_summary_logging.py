"""Тесты summarize.summary_logging."""

import json
from types import SimpleNamespace

from summarize.summary_logging import (
    SummaryCompactionLog,
    append_summary_trace_record,
    log_compaction_started,
    log_memory_patch_skipped_multi_batch,
    log_memory_patch_wm_unchanged,
    log_summary_message_body,
    log_summary_raw_text,
    log_summary_slice_fallback,
    resolve_summary_trace_path,
    trigger_reason_label,
)


def test_summary_slice_fallback_logs_warning():
    messages: list[str] = []

    class _Logger:
        def warning(self, msg: str, *args: object) -> None:
            messages.append(msg % args if args else msg)

    log_ctx = SummaryCompactionLog(trigger_reason="turn_end", thread_id="t1")
    log_summary_slice_fallback(
        _Logger(),
        log_ctx,
        slice_mode="short",
        raw_summary_chars=42,
    )
    assert messages
    assert "summary_slice_fallback" in messages[0]
    assert "short" in messages[0]


def test_memory_patch_wm_unchanged_logs_warning():
    messages: list[str] = []

    class _Logger:
        def warning(self, msg: str, *args: object) -> None:
            messages.append(msg % args if args else msg)

    log_memory_patch_wm_unchanged(
        _Logger(),
        batches=1,
        format_repair_attempted=True,
        format_repair_success=False,
        raw_response_chars=200,
    )
    assert messages
    assert "summary_memory_patch_wm_unchanged" in messages[0]


def test_memory_patch_skipped_multi_batch_logs_info():
    messages: list[str] = []

    class _Logger:
        def info(self, msg: str, *args: object) -> None:
            messages.append(msg % args if args else msg)

    log_memory_patch_skipped_multi_batch(_Logger(), batches=3)
    assert messages
    assert "summary_memory_patch_skipped_multi_batch" in messages[0]


def test_trigger_reason_labels():
    assert trigger_reason_label("turn_end") == "конец хода после turn_finish"
    assert trigger_reason_label("pre_backend") == "середина хода (окно переполнено)"
    assert trigger_reason_label("session_compact") == "по запросу агента (session_compact)"


def test_cli_prints_full_summary_texts(capsys):
    log_ctx = SummaryCompactionLog(
        trigger_reason="pre_run",
        thread_id="t1",
        trigger_volume=100,
        trigger_threshold_chars=50,
        is_cli_mode=True,
    )
    log_compaction_started(
        None,
        log_ctx,
        serialized_chars=120,
        history_messages_count=3,
    )
    log_summary_raw_text(None, log_ctx, "RAW\nLINE")
    log_summary_message_body(None, log_ctx, "BODY\nLINE")

    out = capsys.readouterr().out
    assert "Сжатие: старт сессии" in out
    assert "Полный текст сводки:" in out
    assert "RAW\nLINE" in out
    assert "Тело сообщения dialog_summary:" in out
    assert "BODY\nLINE" in out


def test_resolve_summary_trace_path_prefers_ctx(tmp_path):
    custom = tmp_path / "custom_trace.jsonl"
    ctx = SimpleNamespace(
        summary_trace_path=str(custom),
        config_dict={"save_logs": True, "logs_dump_dir": str(tmp_path / "ignored")},
    )
    assert resolve_summary_trace_path(ctx) == str(custom)


def test_append_summary_trace_record_writes_jsonl(tmp_path):
    trace_file = tmp_path / "trace.jsonl"
    ctx = SimpleNamespace(
        summary_trace_path=str(trace_file),
        config_dict={},
    )
    append_summary_trace_record(
        ctx,
        trigger_reason="turn_end",
        trace_data={"step": 1},
        newly_summarized_message_id="m_sum",
    )
    parsed = json.loads(trace_file.read_text(encoding="utf-8").strip())
    assert parsed["trigger_reason"] == "turn_end"
    assert parsed["summary_trace"] == {"step": 1}
    assert parsed["newly_summarized_message_id"] == "m_sum"
