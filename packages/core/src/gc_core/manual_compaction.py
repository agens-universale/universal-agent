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
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.run_summary_coordinator import RunSummaryCoordinator
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
    config_dict = ctx.config_dict
    backend_api_mode = (
        str(config_dict.get("backend_api_mode", "responses")).strip().lower()
    )
    if backend_api_mode != "chat_completions" or not thread_id:
        return False
    system_id = system or "cli"
    thread_session = await ThreadSession.afrom_config(
        config_dict=config_dict,
        system_id=system_id,
        thread_id=thread_id,
        # Хранилище встраивающего, если он его передал.
        store=getattr(ctx, "thread_store", None),
    )
    thread_session.history_messages = repair_tool_call_pairing(
        thread_session.history_messages,
        keep_latest_unanswered=False,
    )
    meta = getattr(thread_session, "meta", None) or {}
    thread_shared = ThreadMetaMapper.shared_from_meta(meta)

    state = await State.acreate(
        verbose=bool(config_dict.get("verbose", False)),
        model=config_dict["model"],
        tools_spec=ctx.tools_spec,
        tools_descr=ctx.tools_description,
        skills_list=[],
        max_steps=int(config_dict.get("max_steps", 30) or 30),
        system_prompt_id=config_dict.get("system_prompt_id", ""),
        use_server_subagents=bool(
            config_dict.get("use_server_subagents", False)
        ),
        thread_id=thread_id,
        shared=thread_shared,
        workspace_config=config_dict,
    )
    state.backend_api_mode = "chat_completions"
    state.usage = UsageAccount.from_meta(meta.get("token_usage"))
    state.dialog_steps_offset = int(meta.get("dialog_steps_offset", 0) or 0)
    state.init_user_intent_log(meta.get("user_intent_log"))
    state.init_current_user_intent(meta.get("current_user_intent"))
    state.init_user_turn_seq(meta.get("user_turn_seq"))
    state.infer_user_turn_seq_if_missing()
    state.messages = [
        _shallow_copy_msg(m)
        for m in thread_session.history_messages
        if isinstance(m, dict)
    ]

    coord = RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=thread_session,
        summary_trigger_chars=parse_summary_trigger_chars(config_dict),
        is_cli_mode=(system_id == "cli"),
        pre_backend_trigger_reason="pre_backend",
        logger=getattr(ctx, "logger", None),
    )
    ctx.on_summary_applied = coord.on_summary_applied
    await coord.aforce_compact_once(trigger_reason="manual")

    # Компакция сохраняет thread meta из ts.meta (старый token_usage переносится
    # как есть) — поэтому обновлённый кумулятив (baseline + summary) персистим
    # отдельно и обновляем live-state, чтобы TUI-футер сразу увидел рост.
    # ВАЖНО: пишем в TOP-LEVEL ``token_usage`` (его гидрирует run()), НЕ через
    # update_tui_meta — тот кладёт под вложенный meta["tui"], откуда run() не читает
    # → на следующем прогоне summary «пропадал» (откат кумулятива).
    try:
        port = thread_session.port
        persisted_meta = await port.aload_meta(system_id, thread_id) or {}
        persisted_meta["token_usage"] = state.usage.to_meta()
        await port.asave_meta(system_id, thread_id, persisted_meta)
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
    return True
