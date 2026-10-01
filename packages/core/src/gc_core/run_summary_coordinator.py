"""
Координатор summary/pre-backend compaction для одного запуска агента.

См. docs/agent-executor-summary-refactor-spec.md.
"""

from __future__ import annotations

import contextlib
import gc
from typing import TYPE_CHECKING, Any

from gc_core.config_modules.strip_tags_projection import (
    projection_tags_from_config,
)
from gc_core.config_modules.summary import SUMMARY_CHARS_PER_TOKEN
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.message_keys import (
    filter_messages_not_in_state as filter_messages_not_in_state,
)
from gc_core.summarize.compaction import (
    arun_summary_compaction,
    exceeded_summary_triggers,
    history_exceeds_summary_trigger,
)
from gc_core.summarize.summary_logging import (
    SummaryCompactionLog,
    aappend_summary_trace_record,
    log_compaction_failed,
    log_pre_run_max_rounds,
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
    """Прокси в хранилище сессии (I1: не clobber tui).

    Через ``ts.port``, а не через модульную функцию: у сессии хранилище своё —
    файловое по умолчанию или переданное встраивающим. Прямой вызов уводил бы
    флаг на диск даже тогда, когда весь остальной тред лежит в базе.

    ЕДИНСТВЕННАЯ форма: синхронного близнеца звал только синхронный
    ``_summarizing_flag``, а того — только его собственный тест. В проде оба
    были мертвы, и держать их значило держать вторую дорогу к тому же meta.

    Через этот шов идут ТРИ индикатора TUI — ``summarizing``, ``finalizing`` и
    ``finish_gate_checking``, — и возврат у него не используется вовсе. Для
    асинхронного хранилища это самый тихий из отказов: корутина создаётся и
    выбрасывается, исключения нет, в базу не уходит ничего. Индикаторы просто
    никогда не загораются.
    """
    ts = thread_session
    await ts.port.apatch_runtime_flag(
        ts.system_id,
        ts.thread_id,
        flag,
        active=active,
        session_meta=ts.meta,
    )


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
    """Use an optional atomic store batch, retaining the custom-store path."""
    if thread_session is None:
        return
    reset = getattr(thread_session.port, "areset_run_flags", None)
    if callable(reset):
        await reset(
            thread_session.system_id,
            thread_session.thread_id,
            session_meta=thread_session.meta,
        )
        return
    await areset_stale_summarizing_flag(thread_session)
    await aset_tui_finalizing(thread_session, False)
    await aset_tui_finish_gate_checking(thread_session, False)


async def areset_stale_summarizing_flag(thread_session) -> None:
    """Снять залипший ``summarizing`` при старте: форма одна, синхронной нет."""
    if thread_session is None:
        return
    ts = thread_session
    disk = await ts.port.aload_meta(ts.system_id, ts.thread_id) or {}
    rt = (
        (ts.meta.get("runtime") or {})
        if isinstance(ts.meta.get("runtime"), dict)
        else {}
    )
    disk_rt = (
        disk.get("runtime") if isinstance(disk.get("runtime"), dict) else {}
    )
    if rt.get("summarizing") or disk_rt.get("summarizing"):
        await _aset_runtime_flag(ts, "summarizing", False)


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
    _find_function_call_in as _find_function_call_in,
)
from gc_core.execution.preserved_batch import (
    _messages_tail_matches as _messages_tail_matches,
)
from gc_core.execution.preserved_batch import (
    _preserved_function_output_batch as _preserved_function_output_batch,
)
from gc_core.execution.preserved_batch import (
    _preserved_user_ask_batch as _preserved_user_ask_batch,
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
    ) -> None:
        self._ctx = ctx
        self._state = state
        self._thread_session = thread_session
        self._summary_trigger_chars = summary_trigger_chars
        self._turn_end_trigger_chars = turn_end_trigger_chars
        self._summary_trigger_tokens = summary_trigger_tokens
        self._turn_end_trigger_tokens = turn_end_trigger_tokens
        self._is_cli_mode = is_cli_mode
        self._pre_backend_trigger_reason = pre_backend_trigger_reason
        self._logger = logger
        self._proj_tags = projection_tags_from_config(
            getattr(ctx, "config_dict", None)
        )
        # Слепой токенный порог: был ли уже хук (значит, и главный запрос) и
        # сказано ли об этом в лог в этом прогоне.
        self._backend_requested = False
        self._blind_token_trigger_warned = False

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
                    tags=self._proj_tags,
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
                tags=self._proj_tags,
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
        """Компакция прямо перед вызовом backend'а (основная реализация).

        Уезжает в ``arun_agent`` аргументом ``pre_backend_compaction_hook``:
        корутинную функцию цикл ждёт нативно, без потока из пула.
        """
        state = self._state
        if state.backend_api_mode != "chat_completions":
            return
        if (
            self._summary_trigger_chars <= 0
            and self._summary_trigger_tokens <= 0
        ):
            return
        if self._summary_trigger_tokens > 0:
            self._warn_if_token_trigger_blind()
        hits = exceeded_summary_triggers(
            state=state,
            summary_trigger_chars=self._summary_trigger_chars,
            summary_trigger_tokens=self._summary_trigger_tokens,
            tags=self._proj_tags,
            strip_think=True,
        )
        if not hits:
            return
        exceeded_chars, exceeded_tokens = _hit_pair(hits)

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
        if not _messages_tail_matches(state_tail, suffix_exact):
            return

        # Суммаризуем полный ``state.messages`` (включая preserved_batch); суффикс не снимаем
        # до вызова. После компакции снова добавляем preserved_batch (сводка + сырой хвост).
        async with _asummarizing_flag(self._thread_session):
            await arun_summary_compaction(
                ctx=self._ctx,
                state=state,
                trigger_reason=self._pre_backend_trigger_reason,
                is_cli_mode=self._is_cli_mode,
                on_summary_applied=self.on_summary_applied,
                trigger_threshold_chars=self._summary_trigger_chars,
                exceeded_chars=exceeded_chars,
                exceeded_tokens=exceeded_tokens,
            )
        msgs = getattr(state, "messages", None)
        if isinstance(msgs, list):
            msgs.extend(preserved_batch)

        if self._thread_session is not None and preserved_batch:
            await self._thread_session.apersist_batch(
                preserved_batch,
                previous_response_id=state.previous_response_id,
            )

        # После суммаризации + extend следующий шаг обрабатывает только preserved_batch.
        current_messages[:] = preserved_batch
        # C1: старый хвост current_messages отцеплен — собираем циклический мусор.
        self._collect_garbage()

    async def aturn_end_compact_if_needed(self) -> None:
        """Compaction после accept ``turn_finish`` (turn-end trigger).

        Основная и единственная реализация; в ``arun_agent`` уезжает аргументом
        ``turn_end_compaction_hook`` и ждётся нативно.
        """
        state = self._state
        if state.backend_api_mode != "chat_completions":
            return
        if (
            self._turn_end_trigger_chars <= 0
            and self._turn_end_trigger_tokens <= 0
        ):
            return
        hits = exceeded_summary_triggers(
            state=state,
            summary_trigger_chars=self._turn_end_trigger_chars,
            summary_trigger_tokens=self._turn_end_trigger_tokens,
            tags=self._proj_tags,
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
