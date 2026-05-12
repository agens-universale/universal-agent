"""
Координатор summary/pre-backend compaction для одного запуска агента.

См. docs/agent-executor-summary-refactor-spec.md.
"""

from __future__ import annotations

import json
import os
from copy import copy as shallow_copy
from typing import TYPE_CHECKING, Any

from summarize.compaction import (
    history_exceeds_summary_trigger,
    run_summary_compaction,
)
from message_policy.user_message_policy import extract_real_user_message_text
from thread_store import make_message_key

if TYPE_CHECKING:
    from run_context import RunContext
    from states import State
    from thread_store import ThreadSession

_FUNCTION_CALL_TYPES = frozenset({"function_call", "tool_call"})
_FUNCTION_OUTPUT_TYPES = frozenset({"function_call_output", "tool_call_output"})


def remove_exact_suffix_if_matches(messages: list[dict], suffix: list[dict]) -> bool:
    """
    Если ``messages`` заканчивается на последовательность ``suffix``, удалить этот суффикс
    только с хвоста (совпадение по списку и поэлементному ``==``). Иначе не менять историю.

    Не изменяет середину ``messages``.
    """
    if not suffix or len(messages) < len(suffix):
        return False
    if messages[-len(suffix) :] != suffix:
        return False
    del messages[-len(suffix) :]
    return True


def build_preserved_batch(
    *,
    current_messages: list[dict],
    mode: str,
    state_messages: list[dict],
    message_keys_already_persisted: frozenset[str] | None = None,
) -> list[dict]:
    """
    Собирает сообщения текущего backend-вызова, которые нужно сохранить от compaction.

    ``state_messages`` — полный канонический ``state.messages`` на момент вызова
    pre-backend hook (до удаления суффикса текущего батча). Нужна для разрешения
    пар ``function_call`` → ``*_output``.

    Для режима ``user_ask``: если передан непустой ``message_keys_already_persisted``,
    сообщения режутся до **входа текущего шага** — только те элементы ``current_messages``,
    чей ключ не был известен треду до ``persist_batch`` этого шага (уже сохранённые
    ключи считаются повторным контекстом, а не новым входом шага).
    """
    if not current_messages:
        return []
    if mode == "user_ask":
        scoped_messages = current_messages
        if message_keys_already_persisted is not None:
            incoming = [
                m
                for m in current_messages
                if isinstance(m, dict)
                and make_message_key(m) not in message_keys_already_persisted
            ]
            scoped_messages = (
                incoming if incoming else [current_messages[-1]]
            )
        last = scoped_messages[-1]
        out: list[dict] = []
        for msg in scoped_messages[:-1]:
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role", "")).strip().lower() != "user":
                continue
            entry = extract_real_user_message_text(msg)
            if not entry:
                continue
            out.append(msg)
        out.append(last)
        return out
    if mode == "function_call_output":
        outputs: list[dict] = []
        for m in current_messages:
            if isinstance(m, dict) and m.get("type") in _FUNCTION_OUTPUT_TYPES:
                outputs.append(m)
        batches: list[dict] = []
        seen_calls: set[str] = set()
        for output in outputs:
            call_id_raw = output.get("call_id")
            if not isinstance(call_id_raw, str) or not call_id_raw:
                continue
            if call_id_raw in seen_calls:
                continue
            call_msg = find_function_call_for_id(
                call_id_raw,
                current_messages=current_messages,
                state_messages=state_messages,
            )
            if call_msg is None:
                continue
            seen_calls.add(call_id_raw)
            batches.extend((shallow_copy(call_msg), shallow_copy(output)))
        return batches
    return []


def find_function_call_for_id(
    call_id: str,
    *,
    current_messages: list[dict],
    state_messages: list[dict],
) -> dict | None:
    """Сначала ищет вызов в ``current_messages``, затем во всём ``state_messages``."""
    if not isinstance(call_id, str) or not call_id:
        return None
    for seq in (current_messages, state_messages):
        if not seq:
            continue
        for m in seq:
            if not isinstance(m, dict):
                continue
            if m.get("type") not in _FUNCTION_CALL_TYPES:
                continue
            cid = m.get("call_id")
            if cid == call_id:
                return m
    return None


class RunSummaryCoordinator:
    def __init__(
        self,
        *,
        ctx: RunContext,
        state: State,
        thread_session: ThreadSession | None,
        summary_trigger_chars: int,
        is_cli_mode: bool,
        pre_backend_trigger_reason: str,
        logger: Any | None = None,
    ) -> None:
        self._ctx = ctx
        self._state = state
        self._thread_session = thread_session
        self._summary_trigger_chars = summary_trigger_chars
        self._is_cli_mode = is_cli_mode
        self._pre_backend_trigger_reason = pre_backend_trigger_reason
        self._logger = logger

    # --- Trace / archive ---

    def _resolve_trace_path(self) -> str:
        path = getattr(self._ctx, "summary_trace_path", "")
        if isinstance(path, str) and path.strip():
            return path.strip()
        cfg = getattr(self._ctx, "config_dict", {}) or {}
        if not cfg.get("save_logs", False):
            return ""
        base_dir = cfg.get("logs_dump_dir") or os.getcwd()
        return os.path.join(base_dir, "summary_trace.jsonl")

    def _append_summary_trace(
        self,
        *,
        trigger_reason: str,
        trace_data: dict,
        newly_summarized_message_id: str,
    ) -> None:
        trace_path = self._resolve_trace_path()
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

    def on_summary_applied(self, event: dict[str, Any]) -> None:
        if self._thread_session is not None:
            self._thread_session.archive_and_compact_summary_payload(
                event.get("summary_payload", {})
            )
        self._append_summary_trace(
            trigger_reason=str(event.get("trigger_reason") or ""),
            trace_data=event.get("summary_trace", {}),
            newly_summarized_message_id=str(
                event.get("newly_summarized_message_id") or ""
            ),
        )

    def pre_run_compact_if_needed(self, *, trigger_reason: str = "pre_run") -> None:
        logger = self._logger
        state = self._state
        if state.backend_api_mode != "chat_completions" or self._summary_trigger_chars <= 0:
            return
        try:
            if not history_exceeds_summary_trigger(
                state=state,
                summary_trigger_chars=self._summary_trigger_chars,
            ):
                return
            run_summary_compaction(
                ctx=self._ctx,
                state=state,
                trigger_reason=trigger_reason,
                is_cli_mode=self._is_cli_mode,
                on_summary_applied=self.on_summary_applied,
            )
        except Exception as summarize_exc:
            ts = self._thread_session
            if logger is not None:
                if ts is not None:
                    logger.warning(
                        "summary_failed reason=pre_run thread_id=%s error=%s",
                        ts.thread_id,
                        summarize_exc,
                        exc_info=True,
                    )
                else:
                    logger.warning(
                        "summary_failed reason=pre_run error=%s",
                        summarize_exc,
                        exc_info=True,
                    )
            if self._is_cli_mode:
                print(
                    "[summary] Ошибка суммаризации: "
                    f"error={summarize_exc}"
                )

    def pre_backend_compaction_hook(
        self,
        current_messages: list[dict],
        mode: str,
    ) -> None:
        state = self._state
        if state.backend_api_mode != "chat_completions" or self._summary_trigger_chars <= 0:
            return
        if not history_exceeds_summary_trigger(
            state=state,
            summary_trigger_chars=self._summary_trigger_chars,
        ):
            return

        state_tail = getattr(state, "messages", None)
        if not isinstance(state_tail, list):
            return

        keys_before_commit: frozenset[str] | None = None
        ts = self._thread_session
        if ts is not None:
            snap = getattr(ts, "known_keys_snapshot_before_last_batch", None)
            if isinstance(snap, frozenset):
                keys_before_commit = snap

        preserved_batch = build_preserved_batch(
            current_messages=list(current_messages),
            mode=str(mode),
            state_messages=list(state_tail),
            message_keys_already_persisted=keys_before_commit,
        )
        suffix_exact = list(current_messages)
        if not remove_exact_suffix_if_matches(state_tail, suffix_exact):
            return

        try:
            run_summary_compaction(
                ctx=self._ctx,
                state=state,
                trigger_reason=self._pre_backend_trigger_reason,
                is_cli_mode=self._is_cli_mode,
                on_summary_applied=self.on_summary_applied,
            )
        finally:
            msgs = getattr(state, "messages", None)
            if isinstance(msgs, list):
                msgs.extend(preserved_batch)

        if self._thread_session is not None and preserved_batch:
            self._thread_session.persist_batch(
                preserved_batch,
                previous_response_id=state.previous_response_id,
            )

        # Только здесь добираемся после remove_exact_suffix_if_matches + run_summary_compaction
        # (до try/finally множественные return). То есть батч сняли с конца истории и применили
        # суммаризацию; дальнейший шаг должен брать уже preserved_batch — и [] для режима без пар.
        current_messages[:] = preserved_batch
