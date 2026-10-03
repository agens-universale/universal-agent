from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from gc_core.config_modules.strip_tags_projection import (
    projection_tags_from_config,
)
from gc_core.config_modules.summary import SUMMARY_CHARS_PER_TOKEN
from gc_core.intent_log import serialize, serialize_for_meta, tail_preview
from gc_core.loop_detector.detector import clear as clear_loop_detector
from gc_core.message_format.serialization import (
    calc_history_chars_for_trigger,
    calc_messages_chars_total,
)
from gc_core.message_policy.todo_context import (
    render_todo_for_persistent_state,
)
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from gc_core.settings import settings_of
from gc_core.summarize.summary_logging import (
    SummaryCompactionLog,
    log_compaction_completed,
    log_compaction_precheck,
    log_compaction_started,
    log_summary_message_body,
    log_summary_raw_text,
    log_summary_slice_fallback,
    logger_from_ctx,
)
from gc_core.summarize.summary_message_body import (
    build_dialog_summary_content,
    resolve_intent_log_preview_chars,
)
from gc_core.summarize.summary_spec import (
    agent_summary_slice_mode,
    slice_summary,
)
from gc_core.summarize.thread_summarizer import (
    asummarize_dialog_via_chat_completions,
)
from gc_core.summarize.working_memory_gateway import (
    REGISTER_ORDER,
    WorkingMemoryDeltas,
    apply_memory_patch,
    apply_summary_memory_patch,
    coerce_working_memory_deltas,
    read_session_todo_from_state,
    read_snapshot_from_state,
    render_persistent_state_for_prompt,
    render_session_memory_for_summary_message,
)
from gc_core.utils.aio_compat import await_callback
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id_func,
)


def without_system_messages(messages_list: List[dict]) -> List[dict]:
    out: List[dict] = []
    for msg in messages_list:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).strip().lower() == "system":
            continue
        out.append(msg)
    return out


def _emit_messages_evicted(state: Any, dropped: List[dict]) -> None:
    """Best-effort notify host about messages leaving the model context.

    State does not retain ``dropped``; optional ``state.on_messages_evicted``
    may archive them (harbor trajectory store, tests, etc.). Callback errors
    never fail compaction.

    NB: merge-инвариант в ``tests/test_memory_cutover_invariants.py`` запрещает
    в ``src/summarize/`` упоминание legacy-термина memory-шага (того самого,
    что был вытеснен этим callback'ом) — даже в комментариях. Для архива
    на стороне хоста используем нейтральные слова: store, архив, trajectory.
    """
    if not dropped:
        return
    callback = getattr(state, "on_messages_evicted", None)
    if callback is None:
        return
    try:
        callback(list(dropped))
    except Exception:
        logger = getattr(state, "logger", None)
        if logger is not None:
            logger.exception("on_messages_evicted failed")


def _intent_log_preview_chars_from_ctx(ctx: Any | None) -> int:
    if ctx is None:
        return resolve_intent_log_preview_chars(None)
    summary = settings_of(ctx).summary
    return resolve_intent_log_preview_chars(
        {"intent_log_preview_chars": summary.intent_log_preview_chars}
    )


def _ensure_shared(state: Any):
    shared = getattr(state, "shared", None)
    if shared is None:
        from gc_core.states import SharedStateData

        shared = SharedStateData()
        state.shared = shared
    return shared


def _patched_snapshot(
    snapshot,
    memory_append_deltas,
    memory_replace_deltas,
    memory_append_touched,
    memory_replace_touched,
):
    """Снимок памяти после патча summary: replace-патч приоритетнее append-патча."""
    append_deltas = coerce_working_memory_deltas(memory_append_deltas)
    replace_deltas = coerce_working_memory_deltas(memory_replace_deltas)
    append_touched_f = frozenset(memory_append_touched or ())
    replace_touched_f = frozenset(memory_replace_touched or ())
    if replace_touched_f or not replace_deltas.is_empty():
        return apply_summary_memory_patch(
            snapshot,
            replace_deltas,
            replace_touched=replace_touched_f or None,
        )
    if append_touched_f or not append_deltas.is_empty():
        return apply_memory_patch(
            snapshot,
            append_deltas,
            replace_deltas,
            append_touched=append_touched_f or None,
            replace_touched=replace_touched_f or None,
        )
    return snapshot


def _previous_messages(state: Any) -> list:
    messages = getattr(state, "messages", [])
    return list(messages) if isinstance(messages, list) else []


def _first_system_message(messages: list):
    for msg in messages:
        if (
            isinstance(msg, dict)
            and str(msg.get("role", "")).strip().lower() == "system"
        ):
            return msg
    return None


def apply_summary_state_patch(
    *,
    state: Any,
    raw_summary: str,
    last_dialog_steps: str,
    memory_append_deltas: WorkingMemoryDeltas | Dict[str, str] | None = None,
    memory_replace_deltas: WorkingMemoryDeltas | Dict[str, str] | None = None,
    memory_append_touched: Iterable[str] | None = None,
    memory_replace_touched: Iterable[str] | None = None,
    summarized_messages_count: int,
    intent_log_preview_chars: int | None = None,
) -> str:
    shared = _ensure_shared(state)

    snapshot = _patched_snapshot(
        read_snapshot_from_state(state),
        memory_append_deltas,
        memory_replace_deltas,
        memory_append_touched,
        memory_replace_touched,
    )

    wm_dict = {
        name: snapshot.to_dict().get(name, "") for name in REGISTER_ORDER
    }
    shared.working_memory = wm_dict

    session_memory_block = render_session_memory_for_summary_message(snapshot)
    # Чек-лист едет в baseline дословно, как session_memory: после сжатия
    # вызовы session_todo вырезаны, и это единственный канал до модели.
    session_todo_block = render_todo_for_persistent_state(
        read_session_todo_from_state(state)
    )
    closed_records = list(getattr(state, "user_intent_log", []) or [])
    open_records = list(getattr(state, "current_user_intent", []) or [])
    preview_chars = (
        resolve_intent_log_preview_chars(
            {"intent_log_preview_chars": intent_log_preview_chars}
        )
        if intent_log_preview_chars is not None
        else resolve_intent_log_preview_chars(None)
    )
    summary_message_content = build_dialog_summary_content(
        summary_text=str(raw_summary or ""),
        last_dialog_steps=str(last_dialog_steps or ""),
        session_memory_block=session_memory_block,
        session_todo_block=session_todo_block,
        user_intent_log_preview=tail_preview(closed_records, preview_chars),
        current_user_intent_jsonl=serialize(open_records),
    )
    prev_offset = int(getattr(state, "dialog_steps_offset", 0) or 0)
    state.dialog_steps_offset = prev_offset + max(
        0, int(summarized_messages_count or 0)
    )
    state.previous_response_id = None
    # Старое число провайдера сразу снова перешагнуло бы порог.
    state.last_main_prompt_tokens = None
    state.last_main_prompt_message_count = None

    summary_message = {
        "role": "user",
        "content": wrap_technical_user_content(
            (
                f"Summarized context from previous dialogue.\n{summary_message_content.strip()}"
            ),
            technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
        ),
        "id": generate_message_id_func("m_sum"),
    }
    previous_messages = _previous_messages(state)
    system_message = _first_system_message(previous_messages)
    dropped = [m for m in previous_messages if m is not system_message]
    _emit_messages_evicted(state, dropped)
    if isinstance(system_message, dict):
        state.messages = [system_message, summary_message]
    else:
        state.messages = [summary_message]
    return summary_message_content


@dataclass(frozen=True)
class SummaryTriggerHits:
    """Какие включённые пороги точки превышены. Пустой результат — не сжимать."""

    chars: bool = False
    tokens: bool = False

    def __bool__(self) -> bool:
        return self.chars or self.tokens


def _prompt_token_estimate(
    state: Any,
    *,
    tags: Sequence[str],
    strip_think: bool,
) -> int | None:
    """Input последнего главного prompt плюс хвост по курсу ``SUMMARY_CHARS_PER_TOKEN``.

    Числа или границы нет — None, токенное плечо молчит. Хвост — сообщения
    после уже посчитанного prompt; всю историю второй раз не считаем.
    """
    # Цикл: persistence → states → tools → session_compact → этот модуль.
    from gc_core.persistence.thread_meta_mapper import read_optional_nonneg_int

    tokens = read_optional_nonneg_int(
        getattr(state, "last_main_prompt_tokens", None)
    )
    boundary = read_optional_nonneg_int(
        getattr(state, "last_main_prompt_message_count", None)
    )
    if tokens is None or boundary is None:
        return None
    messages = getattr(state, "messages", None)
    history = list(messages) if isinstance(messages, list) else []
    tail = without_system_messages(history[boundary:])
    tail_chars = calc_history_chars_for_trigger(
        tail, tags=tags, strip_think=strip_think
    )
    return tokens + tail_chars // SUMMARY_CHARS_PER_TOKEN


def exceeded_summary_triggers(
    *,
    state: Any,
    summary_trigger_chars: int,
    summary_trigger_tokens: int = 0,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> SummaryTriggerHits:
    """Какие пороги точки превышены. Порог <= 0 в проверку не входит.

    Один вызов на точку: оба превышены — всё равно один проход сжатия.
    """
    chars_hit = history_exceeds_summary_trigger(
        state=state,
        summary_trigger_chars=summary_trigger_chars,
        tags=tags,
        strip_think=strip_think,
    )
    tokens_hit = False
    if summary_trigger_tokens > 0:
        estimate = _prompt_token_estimate(
            state, tags=tags, strip_think=strip_think
        )
        tokens_hit = estimate is not None and estimate > summary_trigger_tokens
    return SummaryTriggerHits(chars=chars_hit, tokens=tokens_hit)


def history_exceeds_summary_trigger(
    *,
    state: Any,
    summary_trigger_chars: int,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> bool:
    if summary_trigger_chars <= 0:
        return False
    full_messages = without_system_messages(getattr(state, "messages", []))
    return (
        calc_history_chars_for_trigger(
            full_messages, tags=tags, strip_think=strip_think
        )
        > summary_trigger_chars
    )


def _fill_trace_final_output(trace: Dict[str, Any], **fields: Any) -> None:
    final_out = trace.get("final_output")
    if isinstance(final_out, dict):
        final_out.update(fields)


def _archive_suffix_of(summary_event: Dict[str, Any]) -> Optional[str]:
    archive_suffix = summary_event.get("archive_suffix")
    if isinstance(archive_suffix, str):
        return archive_suffix.strip() or None
    return None


def _build_summary_payload(
    state: Any, summary_message_content: str
) -> Dict[str, Any]:
    # nested: cycle with persistence → states → tools → summarize
    from gc_core.persistence.schema import serialize_shared_block

    return {
        "summary_message_content": summary_message_content,
        # Весь shared-блок целиком, а не выборка полей: thread_store этим объектом
        # ПОЛНОСТЬЮ заменяет meta["shared"], поэтому ручной список терял остальные
        # поля (confirm_rules, fetch_url_cache, …) — детерминированно при ручном
        # /compact (там нет post-run heal) и при обрыве рана.
        "shared": serialize_shared_block(getattr(state, "shared", None)),
        "user_intent_log": serialize_for_meta(
            list(getattr(state, "user_intent_log", []) or [])
        ),
        "current_user_intent": serialize_for_meta(
            list(getattr(state, "current_user_intent", []) or [])
        ),
        "last_main_prompt_tokens": getattr(
            state, "last_main_prompt_tokens", None
        ),
        "last_main_prompt_message_count": getattr(
            state, "last_main_prompt_message_count", None
        ),
        "user_turn_seq": int(getattr(state, "user_turn_seq", 0) or 0),
        "dialog_steps_offset": int(
            getattr(state, "dialog_steps_offset", 0) or 0
        ),
    }


async def arun_summary_compaction(
    *,
    ctx: Any,
    state: Any,
    trigger_reason: str,
    is_cli_mode: bool,
    on_summary_applied: Optional[Callable[[Dict[str, Any]], None]] = None,
    trigger_threshold_chars: int | None = None,
    exceeded_chars: bool = False,
    exceeded_tokens: bool = False,
) -> None:
    """Сжать историю треда: суммаризация, патч состояния, событие хосту.

    Основная и единственная реализация: синхронное имя ``run_summary_compaction``
    Сетевой шаг один —
    ``asummarize_dialog_via_chat_completions``; всё остальное здесь — чистые
    вычисления над state и логи, поэтому поток на время суммаризации больше
    не занимается.
    """
    logger = logger_from_ctx(ctx)
    thread_id = getattr(state, "thread_id", None)
    tags = projection_tags_from_config(ctx)
    full_messages = without_system_messages(getattr(state, "messages", []))
    trigger_volume = calc_history_chars_for_trigger(
        full_messages, tags=tags, strip_think=True
    )
    serialized_chars = calc_messages_chars_total(
        full_messages, tags=tags, strip_think=True
    )
    log_ctx = SummaryCompactionLog(
        trigger_reason=trigger_reason,
        thread_id=thread_id,
        trigger_volume=trigger_volume,
        trigger_threshold_chars=trigger_threshold_chars,
        is_cli_mode=is_cli_mode,
        exceeded_chars=exceeded_chars,
        exceeded_tokens=exceeded_tokens,
    )
    log_compaction_started(
        logger,
        log_ctx,
        serialized_chars=serialized_chars,
        history_messages_count=len(full_messages),
    )

    source_user_log = list(getattr(state, "user_intent_log", []) or [])
    source_current_intent = list(
        getattr(state, "current_user_intent", []) or []
    )
    memory_snapshot = read_snapshot_from_state(state)
    summary_result = await asummarize_dialog_via_chat_completions(
        ctx=ctx,
        state=state,
        full_history_messages=full_messages,
        persistent_state_block=render_persistent_state_for_prompt(
            memory_snapshot,
            session_todo=read_session_todo_from_state(state),
        ),
        user_intent_log=source_user_log,
        current_user_intent=source_current_intent,
        steps_offset=int(getattr(state, "dialog_steps_offset", 0) or 0),
    )
    raw_summary = str(summary_result.get("raw_summary") or "")
    num_batches = int(summary_result.get("num_batches") or 0)
    trace = summary_result.get("trace")
    memory_patch = trace.get("memory_patch") if isinstance(trace, dict) else {}
    patch_executed = (
        bool(memory_patch.get("executed"))
        if isinstance(memory_patch, dict)
        else False
    )
    agent_slice = agent_summary_slice_mode(
        num_batches=num_batches,
        patch_executed=patch_executed,
    )
    try:
        agent_summary_text = slice_summary(raw_summary, agent_slice)
    except ValueError:
        log_summary_slice_fallback(
            logger,
            log_ctx,
            slice_mode=agent_slice,
            raw_summary_chars=len(raw_summary),
        )
        agent_summary_text = raw_summary
    memory_append_deltas = coerce_working_memory_deltas(
        summary_result.get("memory_append_deltas")
    )
    memory_replace_deltas = coerce_working_memory_deltas(
        summary_result.get("memory_replace_deltas")
    )
    memory_append_touched = summary_result.get("memory_append_touched")
    memory_replace_touched = summary_result.get("memory_replace_touched")
    preview_chars = _intent_log_preview_chars_from_ctx(ctx)
    log_summary_raw_text(logger, log_ctx, raw_summary)

    summary_message_content = apply_summary_state_patch(
        state=state,
        raw_summary=agent_summary_text,
        last_dialog_steps=str(summary_result.get("last_dialog_steps") or ""),
        memory_append_deltas=memory_append_deltas,
        memory_replace_deltas=memory_replace_deltas,
        memory_append_touched=memory_append_touched,
        memory_replace_touched=memory_replace_touched,
        summarized_messages_count=len(full_messages),
        intent_log_preview_chars=preview_chars,
    )
    if isinstance(trace, dict):
        _fill_trace_final_output(
            trace,
            raw_summary=raw_summary,
            agent_summary_slice=agent_slice,
            agent_summary_text=agent_summary_text,
            summary_message_content=summary_message_content,
        )
    log_summary_message_body(logger, log_ctx, summary_message_content)

    summary_payload = _build_summary_payload(state, summary_message_content)
    summary_event: Dict[str, Any] = {
        "trigger_reason": trigger_reason,
        "summary_payload": summary_payload,
        "summary_trace": summary_result.get("trace", {}),
        "newly_summarized_message_id": str(
            state.messages[0].get("id")
            if isinstance(getattr(state, "messages", []), list)
            and state.messages
            else ""
        ),
    }
    if on_summary_applied is not None:
        await await_callback(on_summary_applied(summary_event))

    archive_suffix = _archive_suffix_of(summary_event)

    new_serialized = calc_messages_chars_total(
        without_system_messages(getattr(state, "messages", [])),
        tags=tags,
        strip_think=True,
    )
    ratio = (
        round((new_serialized / serialized_chars), 4)
        if serialized_chars > 0
        else 0.0
    )
    delta_chars = sum(
        len(v) for v in memory_append_deltas.to_dict().values()
    ) + sum(len(v) for v in memory_replace_deltas.to_dict().values())
    log_compaction_completed(
        logger,
        log_ctx,
        serialized_before=serialized_chars,
        serialized_after=new_serialized,
        compression_ratio=ratio,
        num_batches=summary_result.get("num_batches"),
        memory_delta_chars=delta_chars,
        archive_suffix=archive_suffix,
    )
    clear_loop_detector(state)


async def amaybe_run_summary_compaction(
    *,
    ctx: Any,
    state: Any,
    summary_trigger_chars: int,
    trigger_reason: str,
    is_cli_mode: bool,
    on_summary_applied: Optional[Callable[[Dict[str, Any]], None]] = None,
    summary_trigger_tokens: int = 0,
) -> bool:
    """Проверка порогов плюс один запуск сжатия. Основная реализация — эта."""
    logger = logger_from_ctx(ctx)
    thread_id = getattr(state, "thread_id", None)
    if summary_trigger_chars <= 0 and summary_trigger_tokens <= 0:
        return False
    tags = projection_tags_from_config(ctx)
    hits = exceeded_summary_triggers(
        state=state,
        summary_trigger_chars=summary_trigger_chars,
        summary_trigger_tokens=summary_trigger_tokens,
        tags=tags,
        strip_think=True,
    )
    full_messages = without_system_messages(getattr(state, "messages", []))
    trigger_volume = calc_history_chars_for_trigger(
        full_messages, tags=tags, strip_think=True
    )
    serialized_chars = calc_messages_chars_total(
        full_messages, tags=tags, strip_think=True
    )
    log_compaction_precheck(
        logger,
        trigger_reason=trigger_reason,
        thread_id=thread_id,
        history_messages_count=len(full_messages),
        trigger_volume=trigger_volume,
        serialized_chars=serialized_chars,
        threshold=summary_trigger_chars,
        exceeded_chars=hits.chars,
        exceeded_tokens=hits.tokens,
    )
    if not hits:
        return False

    await arun_summary_compaction(
        ctx=ctx,
        state=state,
        trigger_reason=trigger_reason,
        is_cli_mode=is_cli_mode,
        on_summary_applied=on_summary_applied,
        trigger_threshold_chars=summary_trigger_chars,
        exceeded_chars=hits.chars,
        exceeded_tokens=hits.tokens,
    )
    return True
