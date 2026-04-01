import json
import os
from typing import Any, List

from summarize.summary_utils import apply_summary_state_patch
from thread_summarizer import summarize_dialog_via_chat_completions
from thread_store import ThreadSession, calc_messages_chars_total


def _resolve_summary_trace_path(ctx: Any) -> str:
    path = getattr(ctx, "summary_trace_path", "")
    if isinstance(path, str) and path.strip():
        return path.strip()
    cfg = getattr(ctx, "config_dict", {}) or {}
    if not cfg.get("save_logs", False):
        return ""
    base_dir = cfg.get("logs_dump_dir") or os.getcwd()
    return os.path.join(base_dir, "summary_trace.jsonl")


def _append_summary_trace(
    *,
    ctx: Any,
    trigger_reason: str,
    trace_data: dict,
    newly_summarized_message_id: str,
) -> None:
    trace_path = _resolve_summary_trace_path(ctx)
    if not trace_path:
        return
    trace_dir = os.path.dirname(trace_path)
    if trace_dir:
        os.makedirs(trace_dir, exist_ok=True)
    payload = {
        "trigger_reason": trigger_reason,
        "summary_trace": trace_data or {},
        "newly_summarized_message_id": newly_summarized_message_id,
    }
    with open(trace_path, "a", encoding="utf-8") as fd:
        fd.write(json.dumps(payload, ensure_ascii=False))
        fd.write("\n")


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
    thread_session: ThreadSession,
    summary_trigger_chars: int,
    trigger_reason: str,
    is_cli_mode: bool,
) -> bool:
    logger = getattr(ctx, "logger", None)
    full_messages = without_system_messages(thread_session.get_full_history_messages())
    chars_total = calc_messages_chars_total(full_messages)
    if logger is not None:
        logger.info(
            "summary_precheck reason=%s thread_id=%s history_messages=%s chars_total=%s threshold=%s",
            trigger_reason,
            thread_session.thread_id,
            len(full_messages),
            chars_total,
            summary_trigger_chars,
        )
    if chars_total < summary_trigger_chars:
        return False

    if logger is not None:
        logger.info(
            "summary_triggered reason=%s thread_id=%s system=%s chars_total=%s threshold=%s",
            trigger_reason,
            thread_session.thread_id,
            thread_session.system_id,
            chars_total,
            summary_trigger_chars,
        )
    if is_cli_mode:
        print(
            "[summary] Запущена суммаризация: "
            f"thread={thread_session.thread_id}, chars={chars_total}, "
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

    compact_res = thread_session.archive_and_compact_summary_payload(
        {
            "summary_message_content": summary_result.get("summary_message_content", ""),
            "notes_md": (state.notes_md or "") if isinstance(state.notes_md, str) else "",
            "user_messages_log": list(getattr(state, "user_messages_log", []) or []),
            "dialog_steps_offset": int(getattr(state, "dialog_steps_offset", 0) or 0),
        }
    )
    _append_summary_trace(
        ctx=ctx,
        trigger_reason=trigger_reason,
        trace_data=summary_result.get("trace", {}),
        newly_summarized_message_id=str(compact_res.get("newly_summarized_message_id") or ""),
    )

    new_chars = calc_messages_chars_total(
        without_system_messages(thread_session.get_full_history_messages())
    )
    ratio = round((new_chars / chars_total), 4) if chars_total > 0 else 0.0
    if logger is not None:
        logger.info(
            "summary_completed reason=%s thread_id=%s system=%s old_chars=%s new_chars=%s compression_ratio=%s archive_suffix=%s num_batches=%s memory_additions_chars=%s",
            trigger_reason,
            thread_session.thread_id,
            thread_session.system_id,
            chars_total,
            new_chars,
            ratio,
            compact_res.get("archive_suffix"),
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


def maybe_run_summary_compaction_in_memory(
    *,
    ctx: Any,
    state: Any,
    summary_trigger_chars: int,
    trigger_reason: str,
    is_cli_mode: bool,
) -> bool:
    logger = getattr(ctx, "logger", None)
    full_messages = without_system_messages(getattr(state, "messages", []))
    chars_total = calc_messages_chars_total(full_messages)
    if logger is not None:
        logger.info(
            "summary_precheck reason=%s thread_id=%s history_messages=%s chars_total=%s threshold=%s",
            trigger_reason,
            None,
            len(full_messages),
            chars_total,
            summary_trigger_chars,
        )
    if chars_total < summary_trigger_chars:
        return False

    summary_result = summarize_dialog_via_chat_completions(
        ctx=ctx,
        state=state,
        full_history_messages=full_messages,
        current_memory=(state.notes_md or "") if isinstance(state.notes_md, str) else "",
        user_messages_log=getattr(state, "user_messages_log", []),
        steps_offset=int(getattr(state, "dialog_steps_offset", 0) or 0),
    )
    raw_summary = str(summary_result.get("raw_summary") or "")
    memory_additions = summary_result.get("memory_additions", "")
    apply_summary_state_patch(
        state=state,
        summary_message_content=summary_result.get("summary_message_content", ""),
        memory_additions=memory_additions,
        summarized_messages_count=len(full_messages),
    )
    _append_summary_trace(
        ctx=ctx,
        trigger_reason=trigger_reason,
        trace_data=summary_result.get("trace", {}),
        newly_summarized_message_id=str(
            state.messages[0].get("id")
            if isinstance(getattr(state, "messages", []), list) and state.messages
            else ""
        ),
    )
    if logger is not None:
        logger.info(
            "summary_completed reason=%s thread_id=%s old_chars=%s new_chars=%s num_batches=%s",
            trigger_reason,
            None,
            chars_total,
            calc_messages_chars_total(without_system_messages(state.messages)),
            summary_result.get("num_batches"),
        )
    if is_cli_mode:
        print(
            "[summary] Завершена (in-memory): "
            f"old_chars={chars_total}, batches={summary_result.get('num_batches')}"
        )
    return True
