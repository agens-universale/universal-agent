"""
Координатор summary/pre-backend compaction для одного запуска агента.

См. docs/agent-executor-summary-refactor-spec.md.
"""

from __future__ import annotations

import contextlib
import gc
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gc_core.config_modules.strip_tags_projection import (
    projection_tags_from_config,
)
from gc_core.config_modules.summary import SUMMARY_CHARS_PER_TOKEN
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.message_keys import (
    filter_messages_not_in_state as filter_messages_not_in_state,
)
from gc_core.execution.ports import port
from gc_core.message_format.serialization import calc_history_chars_for_trigger
from gc_core.summarize.compaction import (
    arun_summary_compaction,
    exceeded_summary_triggers,
    history_exceeds_summary_trigger,
    without_system_messages,
)
from gc_core.summarize.config import ResolvedSummaryLimits
from gc_core.summarize.summary_logging import (
    SummaryCompactionLog,
    aappend_summary_trace_record,
    log_compaction_failed,
    log_pre_run_max_rounds,
    log_pre_run_no_progress,
    log_token_trigger_no_prompt_tokens,
)

if TYPE_CHECKING:
    from gc_core.run_context import RunContext
    from gc_core.states import State
    from gc_core.thread_store import ThreadSession


def _hit_pair(hits: Any) -> tuple[bool, bool]:
    """Bool из старых подмен предиката — символьное плечо. Объект — оба флага."""
    if isinstance(hits, bool):
        return hits, False
    return bool(getattr(hits, "chars", False)), bool(
        getattr(hits, "tokens", False)
    )


_FUNCTION_CALL_TYPES = frozenset({"function_call", "tool_call"})
_FUNCTION_OUTPUT_TYPES = frozenset(
    {"function_call_output", "tool_call_output"}
)

# Страховка pre_run: в штате хватает 1 прохода; лимит на случай «лапши», которая после сжатия всё ещё выше порога.
_PRE_RUN_SUMMARY_MAX_ROUNDS = 20


async def _aset_runtime_flag(thread_session, flag: str, active: bool) -> None:
    """Индикатор TUI в живом статусе треда — через сессию (её хранилище и
    её аренда): ``summarizing``, ``finalizing``, ``finish_gate_checking``."""
    await thread_session.aset_flag(flag, active=active)


@contextlib.asynccontextmanager
async def _asummarizing_flag(thread_session):
    """Помечает тред как «идёт суммаризация» для TUI-индикатора.

    Флаг ``meta.runtime.summarizing`` + немедленный flush (TUI поллит meta).
    Снимается строго в finally — иначе индикатор залипнет.
    ``thread_session=None`` (CLI без треда) → no-op.
    """
    if thread_session is None:
        yield
        return
    ts = thread_session
    try:
        await _aset_runtime_flag(ts, "summarizing", True)
        yield
    finally:
        await _aset_runtime_flag(ts, "summarizing", False)


async def areset_run_flags(thread_session) -> None:
    """Снять флаги хода, залипшие после падения прошлого прогона."""
    if thread_session is None:
        return
    await thread_session.areset_run_flags()


async def aset_tui_finalizing(thread_session, active: bool) -> None:
    """Асинхронный двойник ``set_tui_finalizing``."""
    if thread_session is None:
        return
    await _aset_runtime_flag(thread_session, "finalizing", active)


async def aset_tui_finish_gate_checking(thread_session, active: bool) -> None:
    """Асинхронный двойник ``set_tui_finish_gate_checking``."""
    if thread_session is None:
        return
    await _aset_runtime_flag(thread_session, "finish_gate_checking", active)


from gc_core.execution.preserved_batch import (
    _messages_tail_matches,
)
from gc_core.execution.preserved_batch import (
    build_preserved_batch as build_preserved_batch,
)
from gc_core.execution.preserved_batch import (
    find_function_call_for_id as find_function_call_for_id,
)
from gc_core.execution.preserved_batch import (
    remove_exact_suffix_if_matches as remove_exact_suffix_if_matches,
)


class RunSummaryCoordinator:
    def __init__(
        self,
        *,
        ctx: RunContext,
        state: State,
        thread_session: ThreadSession | None,
        summary_trigger_chars: int,
        turn_end_trigger_chars: int = 0,
        summary_trigger_tokens: int = 0,
        turn_end_trigger_tokens: int = 0,
        is_cli_mode: bool,
        pre_backend_trigger_reason: str,
        logger: Any | None = None,
        limits_provider: Callable[[], ResolvedSummaryLimits] | None = None,
        deferred_startup_compaction: bool = False,
        startup_progress_guard: bool = True,
    ) -> None:
        self._ctx = ctx
        self._state = state
        self._thread_session = thread_session
        self._summary_trigger_chars = summary_trigger_chars
        self._turn_end_trigger_chars = turn_end_trigger_chars
        self._summary_trigger_tokens = summary_trigger_tokens
        self._turn_end_trigger_tokens = turn_end_trigger_tokens
        self._limits_provider = limits_provider
        self._startup_compaction_pending = deferred_startup_compaction
        self._startup_progress_guard = startup_progress_guard
        self._is_cli_mode = is_cli_mode
        self._pre_backend_trigger_reason = pre_backend_trigger_reason
        self._logger = logger
        # Теги читаются при сжатии, не в конструкторе: нулевой порог — no-op,
        # и контекст без настроек (заглушка ребёнка) до settings_of не доходит.
        self._proj_tags: tuple[str, ...] | None = None
        # Слепой токенный порог: был ли уже хук (значит, и главный запрос) и
        # сказано ли об этом в лог в этом прогоне.
        self._backend_requested = False
        self._blind_token_trigger_warned = False

    def _refresh_limits(self) -> None:
        """Пороги срабатывания на момент проверки: от провайдера модели истории;
        без провайдера остаётся снимок конструктора. Батч и лимит диалога сюда не
        входят — их считает само сжатие (`compaction.arun_summary_compaction`)."""
        if self._limits_provider is None:
            return
        limits = self._limits_provider()
        self._summary_trigger_chars = limits.chars_during
        self._summary_trigger_tokens = limits.tokens_during
        self._turn_end_trigger_chars = limits.chars_after
        self._turn_end_trigger_tokens = limits.tokens_after

    def _projection_tags(self) -> tuple[str, ...]:
        if self._proj_tags is None:
            self._proj_tags = projection_tags_from_config(self._ctx)
        return self._proj_tags

    def _collect_garbage(self) -> None:
        collect = getattr(self._ctx, "collect_garbage", None) or gc.collect
        collect()

    async def on_summary_applied(self, event: dict[str, Any]) -> None:
        if self._thread_session is not None:
            archive_result = await self._thread_session.aarchive_and_compact_summary_payload(
                event.get("summary_payload", {})
            )
            suffix = archive_result.get("archive_suffix")
            if isinstance(suffix, str) and suffix.strip():
                event["archive_suffix"] = suffix.strip()
        await aappend_summary_trace_record(
            self._ctx,
            trigger_reason=str(event.get("trigger_reason") or ""),
            trace_data=event.get("summary_trace", {}),
            newly_summarized_message_id=str(
                event.get("newly_summarized_message_id") or ""
            ),
        )

    async def apre_run_compact_if_needed(
        self, *, trigger_reason: str = "pre_run"
    ) -> None:
        """Догнать историю до порога перед стартом run'а.

        Единственная форма: сетевой шаг внутри ``arun_summary_compaction``
        ждётся нативно, поэтому компакция не занимает поток из пула на всё
        время суммаризации.
        """
        logger = self._logger
        state = self._state
        if state.backend_api_mode != "chat_completions":
            return
        self._refresh_limits()
        if (
            self._summary_trigger_chars <= 0
            and self._summary_trigger_tokens <= 0
        ):
            return
        rounds = 0
        try:
            while True:
                hits = exceeded_summary_triggers(
                    state=state,
                    summary_trigger_chars=self._summary_trigger_chars,
                    summary_trigger_tokens=self._summary_trigger_tokens,
                    tags=self._projection_tags(),
                    strip_think=True,
                )
                if not hits:
                    break
                rounds += 1
                if rounds > _PRE_RUN_SUMMARY_MAX_ROUNDS:
                    log_pre_run_max_rounds(
                        logger,
                        thread_id=getattr(
                            self._thread_session, "thread_id", None
                        ),
                        max_rounds=_PRE_RUN_SUMMARY_MAX_ROUNDS,
                    )
                    break
                exceeded_chars, exceeded_tokens = _hit_pair(hits)
                async with _asummarizing_flag(self._thread_session):
                    await arun_summary_compaction(
                        ctx=self._ctx,
                        state=state,
                        trigger_reason=trigger_reason,
                        is_cli_mode=self._is_cli_mode,
                        on_summary_applied=self.on_summary_applied,
                        trigger_threshold_chars=self._summary_trigger_chars,
                        exceeded_chars=exceeded_chars,
                        exceeded_tokens=exceeded_tokens,
                    )
                # Число забыто сжатием. Символы выключены — спросить провайдера
                # некого, следующий круг вёл бы только их.
                if self._summary_trigger_chars <= 0:
                    break
                self._refresh_limits()
            # C1: старая история отцеплена компакцией — собираем циклический мусор.
            if rounds > 0:
                self._collect_garbage()
        except ExecutionFailure:
            raise
        except Exception as summarize_exc:
            log_compaction_failed(
                logger,
                SummaryCompactionLog(
                    trigger_reason=trigger_reason,
                    thread_id=getattr(self._thread_session, "thread_id", None)
                    or getattr(state, "thread_id", None),
                    is_cli_mode=self._is_cli_mode,
                ),
                summarize_exc,
            )

    async def aforce_compact_once(
        self, *, trigger_reason: str = "manual"
    ) -> None:
        """Один безусловный проход сжатия (ручной /compact) — БЕЗ проверки порога.
        Та же машинерия, что и автосжатие; персист через on_summary_applied.

        Основная и единственная реализация; синхронное имя ``force_compact_once``"""
        state = self._state
        if state.backend_api_mode != "chat_completions":
            return
        self._refresh_limits()
        async with _asummarizing_flag(self._thread_session):
            await arun_summary_compaction(
                ctx=self._ctx,
                state=state,
                trigger_reason=trigger_reason,
                is_cli_mode=self._is_cli_mode,
                on_summary_applied=self.on_summary_applied,
                trigger_threshold_chars=self._summary_trigger_chars,
            )
        # C1: старая история отцеплена компакцией — собираем циклический мусор.
        self._collect_garbage()

    def _warn_if_token_trigger_blind(self) -> None:
        """Раз за прогон сказать в лог, что токенный порог не может сработать.

        Зовётся в начале pre-backend хука. Числа нет и после сжатия (любого, в том
        числе мимо координатора), поэтому слепым порог признаётся, только когда
        история по символам уже выше него. Первый хук прогона пропускается —
        число ещё не успело прийти.
        """
        if (
            self._backend_requested
            and not self._blind_token_trigger_warned
            and getattr(self._state, "last_main_prompt_tokens", None) is None
            and history_exceeds_summary_trigger(
                state=self._state,
                summary_trigger_chars=self._summary_trigger_tokens
                * SUMMARY_CHARS_PER_TOKEN,
                tags=self._projection_tags(),
                strip_think=True,
            )
        ):
            self._blind_token_trigger_warned = True
            log_token_trigger_no_prompt_tokens(
                self._logger,
                thread_id=getattr(self._thread_session, "thread_id", None),
                threshold=self._summary_trigger_tokens,
            )
        self._backend_requested = True

    async def apre_backend_compaction_hook(
        self,
        current_messages: list[dict],
        mode: str,
    ) -> None:
        """Сжатие после выбора модели; первый вызов догоняет стартовую историю."""
        startup = self._startup_compaction_pending
        self._startup_compaction_pending = False
        reason = "pre_run" if startup else self._pre_backend_trigger_reason
        rounds = _PRE_RUN_SUMMARY_MAX_ROUNDS if startup else 1
        guard_progress = startup and self._startup_progress_guard
        for done in range(1, rounds + 1):
            before_chars = (
                self._history_chars(self._state.messages)
                if guard_progress
                else 0
            )
            compacted = await self._acompact_backend_batch(
                current_messages, mode, reason
            )
            if (
                not compacted
                or not startup
                or self._summary_trigger_chars <= 0
            ):
                return
            if guard_progress and self._startup_stalled(
                current_messages, before_chars
            ):
                self._log_startup_stop(done)
                return
        if self._startup_trigger_exceeded():
            log_pre_run_max_rounds(
                self._logger,
                thread_id=getattr(self._thread_session, "thread_id", None),
                max_rounds=_PRE_RUN_SUMMARY_MAX_ROUNDS,
            )

    def _startup_stalled(
        self, current_messages: list[dict], before_chars: int
    ) -> bool:
        """Нет прогресса: текущий батч сам выше порога или история не сократилась."""
        return (
            self._history_chars(current_messages) > self._summary_trigger_chars
            or self._history_chars(self._state.messages) >= before_chars
        )

    def _log_startup_stop(self, rounds: int) -> None:
        """Ранняя остановка стартового сжатия — в лог, если порог всё ещё превышен."""
        if self._startup_trigger_exceeded():
            log_pre_run_no_progress(
                self._logger,
                thread_id=getattr(self._thread_session, "thread_id", None),
                rounds=rounds,
            )

    def _startup_trigger_exceeded(self) -> bool:
        return bool(
            exceeded_summary_triggers(
                state=self._state,
                summary_trigger_chars=self._summary_trigger_chars,
                summary_trigger_tokens=self._summary_trigger_tokens,
                tags=self._projection_tags(),
                strip_think=True,
            )
        )

    def _history_chars(self, messages: list[dict]) -> int:
        return calc_history_chars_for_trigger(
            without_system_messages(messages),
            tags=self._projection_tags(),
            strip_think=True,
        )

    async def _acompact_backend_batch(
        self, current_messages: list[dict], mode: str, trigger_reason: str
    ) -> bool:
        """Сжать полный контекст, сохранив текущий батч для отправки модели."""
        state = self._state
        hits = self._backend_batch_trigger_hits()
        if not hits:
            return False
        exceeded_chars, exceeded_tokens = _hit_pair(hits)

        state_tail = getattr(state, "messages", None)
        if not isinstance(state_tail, list):
            return False

        preserved_batch = build_preserved_batch(
            current_messages=list(current_messages),
            mode=str(mode),
            state_messages=list(state_tail),
            message_keys_already_persisted=self._keys_persisted_before_last_batch(),
        )
        suffix_exact = list(current_messages)
        if not _messages_tail_matches(state_tail, suffix_exact):
            return False

        # State заменяется до callback: текущий батч должен вернуться даже
        # при сбое трассы, снятия флага или отмене после применения сводки.
        completed = False
        try:
            async with _asummarizing_flag(self._thread_session):
                await arun_summary_compaction(
                    ctx=self._ctx,
                    state=state,
                    trigger_reason=trigger_reason,
                    is_cli_mode=self._is_cli_mode,
                    on_summary_applied=self.on_summary_applied,
                    trigger_threshold_chars=self._summary_trigger_chars,
                    exceeded_chars=exceeded_chars,
                    exceeded_tokens=exceeded_tokens,
                    kept_messages=preserved_batch,
                )
            completed = True
        finally:
            msgs = getattr(state, "messages", None)
            if completed or self._restore_after_failed_compaction(
                msgs, state_tail
            ):
                if isinstance(msgs, list):
                    msgs.extend(
                        filter_messages_not_in_state(msgs, preserved_batch)
                    )
                if self._thread_session is not None and preserved_batch:
                    await self._thread_session.apersist_batch(
                        preserved_batch,
                        previous_response_id=state.previous_response_id,
                    )
                current_messages[:] = preserved_batch
                self._collect_garbage()
        return True

    def _backend_batch_trigger_hits(self) -> Any:
        """Сработавшие триггеры сжатия перед вызовом бэкенда; пусто — сжимать
        не нужно (не `chat_completions` или триггеры выключены)."""
        if self._state.backend_api_mode != "chat_completions":
            return None
        self._refresh_limits()
        if (
            self._summary_trigger_chars <= 0
            and self._summary_trigger_tokens <= 0
        ):
            return None
        if self._summary_trigger_tokens > 0:
            self._warn_if_token_trigger_blind()
        return exceeded_summary_triggers(
            state=self._state,
            summary_trigger_chars=self._summary_trigger_chars,
            summary_trigger_tokens=self._summary_trigger_tokens,
            tags=self._projection_tags(),
            strip_think=True,
        )

    def _keys_persisted_before_last_batch(self) -> frozenset[str] | None:
        snap = getattr(
            self._thread_session, "known_keys_snapshot_before_last_batch", None
        )
        return snap if isinstance(snap, frozenset) else None

    def _restore_after_failed_compaction(
        self, msgs: Any, state_tail: list
    ) -> bool:
        """Возвращать ли батч после сбоя сжатия. История не заменена — нет:
        батч в ней остался; заменена — решает патч исполнения (durable-истории
        до патча возврата не делали)."""
        if not isinstance(msgs, list) or msgs is state_tail:
            return False
        execution_patch = port(self._ctx, "execution_patch")
        return execution_patch is None or execution_patch(
            "summary-batch-preservation-v1"
        )

    async def aturn_end_compact_if_needed(self) -> None:
        """Compaction после accept ``turn_finish`` (turn-end trigger).

        Основная и единственная реализация; в ``arun_agent`` уезжает аргументом
        ``turn_end_compaction_hook`` и ждётся нативно.
        """
        state = self._state
        if state.backend_api_mode != "chat_completions":
            return
        self._refresh_limits()
        if (
            self._turn_end_trigger_chars <= 0
            and self._turn_end_trigger_tokens <= 0
        ):
            return
        hits = exceeded_summary_triggers(
            state=state,
            summary_trigger_chars=self._turn_end_trigger_chars,
            summary_trigger_tokens=self._turn_end_trigger_tokens,
            tags=self._projection_tags(),
            strip_think=True,
        )
        if not hits:
            return
        exceeded_chars, exceeded_tokens = _hit_pair(hits)

        logger = self._logger
        try:
            async with _asummarizing_flag(self._thread_session):
                await arun_summary_compaction(
                    ctx=self._ctx,
                    state=state,
                    trigger_reason="turn_end",
                    is_cli_mode=self._is_cli_mode,
                    on_summary_applied=self.on_summary_applied,
                    trigger_threshold_chars=self._turn_end_trigger_chars,
                    exceeded_chars=exceeded_chars,
                    exceeded_tokens=exceeded_tokens,
                )
            # C1: старая история отцеплена компакцией — собираем циклический мусор.
            self._collect_garbage()
        except ExecutionFailure:
            raise
        except Exception as summarize_exc:
            log_compaction_failed(
                logger,
                SummaryCompactionLog(
                    trigger_reason="turn_end",
                    thread_id=getattr(self._thread_session, "thread_id", None)
                    or getattr(state, "thread_id", None),
                    is_cli_mode=self._is_cli_mode,
                ),
                summarize_exc,
            )
