"""Форс-сжатие персистентного треда вне run-loop (ручной /compact).

Оркестрация суммаризации (не исполнения): грузит тред, строит минимальный
state под компакцию, зовёт RunSummaryCoordinator.aforce_compact_once.
Сосед run_summary_coordinator.py; из контроллера AgentExecutor вынесено.

Форма одна — асинхронная (``acompact_thread``). Синхронного имени нет:
из синхронного кода ведут своим ``asyncio.run`` (так делает TUI).
"""

from __future__ import annotations

from copy import copy as _shallow_copy_msg
from threading import Lock
from typing import Any

from gc_core.message_policy.chat_completions import repair_tool_call_pairing
from gc_core.persistence.session_state import (
    hydrate_state,
    shared_from_session,
)
from gc_core.run_summary_coordinator import RunSummaryCoordinator
from gc_core.settings import settings_of
from gc_core.states import State
from gc_core.summarize.config import parse_summary_trigger_chars
from gc_core.thread_store import ThreadSession
from gc_core.usage.account import UsageAccount


async def acompact_thread(
    ctx: Any,
    thread_id: str,
    _lock: Lock | None = None,
    self_state: State | None = None,
    system: str | None = None,
) -> bool:
    """chat_completions + непустой thread_id → сжатие выполнено (True); иначе False.

    Основная реализация: суммаризация уезжает в координатор нативно
    (``aforce_compact_once``), без потока из пула на всё время LLM-вызова.
    Синхронный двойник — ``compact_thread`` ниже.
    """
    settings = settings_of(ctx)
    backend_api_mode = str(settings.backend.api_mode).strip().lower()
    if backend_api_mode != "chat_completions" or not thread_id:
        return False
    system_id = system or "cli"
    thread_session = await ThreadSession.afrom_config(
        settings=ctx,
        system_id=system_id,
        thread_id=thread_id,
        # Хранилище встраивающего, если он его передал.
        store=getattr(ctx, "thread_store", None),
        # Сжатие переписывает историю: тред, который ведёт ход, — ThreadBusy.
        claim=True,
    )
    try:
        return await _acompact_session(
            ctx, thread_session, _lock=_lock, self_state=self_state
        )
    finally:
        await thread_session.arelease_lease()


async def _acompact_session(
    ctx: Any,
    thread_session: ThreadSession,
    *,
    _lock: Lock | None,
    self_state: State | None,
) -> bool:
    """Сжатие загруженного и взятого треда."""
    settings = settings_of(ctx)
    system_id = thread_session.system_id
    thread_id = thread_session.thread_id
    thread_session.history_messages = repair_tool_call_pairing(
        thread_session.history_messages,
        keep_latest_unanswered=False,
    )
    thread_shared = shared_from_session(
        thread_session.state, thread_session.extra
    )

    state = await State.acreate(
        verbose=settings.agent.verbose,
        model=settings.agent.model,
        tools_spec=ctx.tools_spec,
        tools_descr=ctx.tools_description,
        skills_list=[],
        max_steps=settings.agent.max_steps or 30,
        system_prompt_id=settings.agent.system_prompt_id,
        use_server_subagents=settings.agent.use_server_subagents,
        thread_id=thread_id,
        shared=thread_shared,
        workspace_config=ctx,
    )
    state.backend_api_mode = "chat_completions"
    hydrate_state(
        state,
        thread_session.state,
        thread_session.usage,
        thread_session.notice,
    )
    state.messages = [
        _shallow_copy_msg(m)
        for m in thread_session.history_messages
        if isinstance(m, dict)
    ]

    coord = RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=thread_session,
        summary_trigger_chars=parse_summary_trigger_chars(settings),
        is_cli_mode=(system_id == "cli"),
        pre_backend_trigger_reason="pre_backend",
        logger=getattr(ctx, "logger", None),
    )
    ctx.on_summary_applied = coord.on_summary_applied
    await coord.aforce_compact_once(trigger_reason="manual")

    # Компакция пишет расход из снимка сессии (старый кумулятив) — поэтому
    # обновлённый кумулятив (baseline + summary) персистим отдельным шагом и
    # обновляем live-state, чтобы TUI-футер сразу увидел рост. Пишем в часть
    # ``Usage`` (её гидрирует run()), а не в секцию TUI, откуда run() не
    # читает: иначе на следующем прогоне summary «пропадал».
    try:
        from gc_core.persistence.session_state import usage_from_state

        await thread_session.apersist_batch(
            [],
            previous_response_id=thread_session.previous_response_id,
            session=thread_session.state,
            usage=usage_from_state(state),
        )
    except Exception:
        pass
    # Обновляем live-state (что читает TUI-футер) только когда он передан и это тот
    # же тред — под замком executor'а. Для программного/standalone вызова (_lock или
    # self_state = None) шаг пропускается.
    if (
        _lock is not None
        and self_state is not None
        and getattr(self_state, "thread_id", None) == thread_id
    ):
        with _lock:
            self_state.usage = UsageAccount.from_meta(state.usage.to_meta())
            self_state.last_main_prompt_tokens = None
            self_state.last_main_prompt_message_count = None
    return True
