"""
Потокобезопасный исполнитель агента.

Один мьютекс на экземпляр, держится кратко (доступ к state / pending / stop).
State создаётся заново при каждом run(). Возвращённый из get_state() state
нельзя мутировать снаружи; изменения — только через add_message() или под замком executor'а.
"""

import asyncio
import functools
import gc
import threading
from copy import copy as _shallow_copy_msg
from typing import Any, Optional

from gc_core.agent_edits.snapshots import purge_thread_snapshots
from gc_core.attachments.model import Attachment, attachments_of
from gc_core.attachments.store import (
    commit_to_run_thread,
    with_committed_attachments,
)
from gc_core.auto_routing import state_ops
from gc_core.config_modules.stored_outputs import (
    get_stored_tool_outputs_max_entries,
)
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.message_keys import withdraw_from_queue
from gc_core.execution.ports import port
from gc_core.execution.turn_clock import current_turn_clock
from gc_core.execution_workspace import launch_root_from_config
from gc_core.intent_log.user_turn import (
    append_user_ask_deduped,
    append_user_asks_from_messages,
)
from gc_core.mcp_session_owner import reset_owner, set_owner
from gc_core.message_policy.chat_completions import repair_tool_call_pairing
from gc_core.message_policy.plan_messages import (
    PLAN_REMOVED_MESSAGE,
    accepted_plan_messages,
)
from gc_core.message_policy.stop_persist import is_persist_discarded
from gc_core.message_policy.tool_usage_notice.state import (
    persist_tool_usage_notice_state,
)
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
    wrap_technical_user_content,
)
from gc_core.model_choice import note_applied_at_run_end
from gc_core.persistence.model_choice_record import ModelChoiceRecord
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.persistence.turn_outcome import (
    CANCELLED,
    USER_STOP,
    TurnOutcome,
    TurnStatus,
)
from gc_core.plan_store import (
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_REWORKING,
    normalize_plan_block,
    now_stamp,
)
from gc_core.processing_responses import (
    _acall_blocking_hook,
    arun_agent,
    begin_turn_main_trace,
)
from gc_core.run_context import RunContext
from gc_core.run_summary_coordinator import (
    RunSummaryCoordinator,
    areset_run_flags,
    aset_tui_finalizing,
    aset_tui_finish_gate_checking,
)
from gc_core.security.action_judge_step import lift_pause_on_user_message
from gc_core.security.permission_mode import (
    PLAN_EXECUTION_MODES,
    Mode,
    coerce_mode,
    resolve_mode,
)
from gc_core.security.permission_store import PermissionStore
from gc_core.settings import settings_of
from gc_core.states import SharedStateData, State
from gc_core.summarize.config import (
    resolve_summary_limits_at_level,
)
from gc_core.thread_store import (
    ThreadSession,
    make_message_key,
    resolve_threads_dir,
)
from gc_core.tools.connect_mcp.persistent_stdio import PersistentStdioRegistry
from gc_core.tools.connect_mcp.stream_client import (
    _SESSION_MANAGER as _STREAM_SESSION_MANAGER,
)
from gc_core.tools.subagents.active_scope import (
    active_parent_call_id,
    is_child_input_active,
    try_enqueue_user_message,
)
from gc_core.tools.subagents.selection import subagents_enabled
from gc_core.tools.subagents.sidecar import (
    bind_thread_identity,
    bind_turn_identity,
    clear_thread_identity,
    mint_turn_id,
)
from gc_core.trace_id import trace_id_from_config
from gc_core.turn_boundary import opening_reply_id
from gc_core.turn_error import describe_turn_error
from gc_core.usage.account import UsageAccount
from gc_core.usage.report import build_run_usage_dict, build_run_usage_report
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id_func,
)
from gc_core.utils.local_render_system_prompt import render_system_prompt
from gc_core.utils.logging_module import human_console_enabled

ORCHESTRATOR_SPECIFIC_ROLE = (
    "You are an orchestrator agent. You may execute work yourself or delegate to subagents when that clearly pays off.\n"
    "- Handle simple, local tasks yourself (small edits, one-off questions, quick checks) — do not delegate by default.\n"
    "- Delegate only when the task is genuinely complex, parallelizable, or needs a specialized role "
    "(e.g. deep review, large exploration) and delegation tools are listed in `<tools>`.\n"
    "- For complex work, decompose into subtasks and delegate appropriately; validate outcomes yourself "
    "or via a review subagent when quality or risk warrants it.\n"
    "- At the start of a large task, use a planner subagent only when planning overhead is justified.\n"
)


#: ``State.terminal_outcome``, которыми прогон остановлен лимитом: ход
#: закрыт исходом ``stopped`` с этой причиной.
_LIMIT_OUTCOMES = frozenset(
    {
        "step_limit",
        "tool_call_limit",
        "token_limit",
        "tool_errors",
        "judge_unavailable",
    }
)


async def _aappend_trace_id_to_meta(
    thread_session: Any, trace_id: str, logger: Any | None = None
) -> None:
    """Дописать trace_id в ``meta.runtime.trace_ids`` (append + дедуп).

    Через ``update_runtime_meta`` (read-merge-write по диску), а НЕ через
    ``thread_session.meta``: ``persist_batch`` пересобирает base_meta с диска и
    переносит из ``self.meta`` только whitelist ключей, в который ``runtime`` не
    входит — запись в память была бы потеряна.

    Отвечает на вопрос аудита «какие шаги оркестратора трогали этот тред».
    Ошибки бухгалтерии меты не должны ронять ран, но раз это единственный путь
    записи trace_id в мету — молчаливый swallow оставляем, а факт и причину
    ошибки пишем в debug (``logger`` опционален, чтобы не менять контракт
    вызывающих сторон).
    """
    from gc_core.persistence.meta_ownership import runtime_section
    from gc_core.thread_store import sanitize_system_id

    # Хранилище берём у самой сессии: у неё оно уже своё — файловое по
    # умолчанию или переданное встраивающим. Прямой вызов модульных функций
    # уводил бы записи на диск даже тогда, когда история лежит в базе.
    store = getattr(thread_session, "store", None)
    port = getattr(thread_session, "port", None)
    system_id = getattr(thread_session, "system_id", None)
    thread_id = getattr(thread_session, "thread_id", None)
    if store is None or port is None or not system_id or not thread_id:
        return
    try:
        sys_norm = sanitize_system_id(system_id)
        meta = await port.aload_meta(sys_norm, thread_id) or {}
        existing = runtime_section(meta).get("trace_ids")
        ids = [str(x) for x in existing] if isinstance(existing, list) else []
        if trace_id in ids:
            return
        ids.append(trace_id)
        await port.aupdate_runtime_meta(
            sys_norm, thread_id, {"trace_ids": ids}
        )
    except ExecutionFailure:
        raise
    except Exception as exc:
        if logger is not None:
            try:
                logger.debug(
                    "trace_id_meta_write_failed error=%s", exc, exc_info=True
                )
            except Exception:
                pass
        return


async def _aclose_mcp_and_release(owner_reset: Any) -> None:
    """Уборка после прогона: stdio-сессии, stream-сессии, владение.

    Отмену КОПИМ, а не пробрасываем сразу. Раньше `CancelledError` из первого
    шага уносил управление мимо всего остального: не закрывались
    stream-сессии, не снималось владение. Отмена приходит ровно тогда, когда
    прибираться нужнее всего — по стопу пользователя или дедлайну, — и именно
    тогда браузер оставался жить. `except Exception` её не ловит: она
    `BaseException`.

    Вынесено из `finally` отдельной функцией не ради красоты: внутри `finally`
    это свойство проверялось бы только копией блока в тесте, то есть никак.
    """
    cancelled: Optional[BaseException] = None
    try:
        # Долгоживущие stdio (напр. @playwright/mcp + Chromium): владелец
        # берётся из contextvar здесь же — закроется только наше, чужой
        # браузер продолжит работать.
        await PersistentStdioRegistry.instance().aclose_all()
    except asyncio.CancelledError as exc:
        cancelled = exc
    except Exception:
        pass
    try:
        # НЕ через to_thread: сессии ключуются по id(loop) и живут на ЭТОМ
        # loop — из чужого потока их не видно и закрыть нечем.
        await _STREAM_SESSION_MANAGER.aclose_all()
    except asyncio.CancelledError as exc:
        cancelled = cancelled or exc
    except Exception:
        pass
    # C2: после закрытия MCP/stdio — подбираем циклический мусор
    # (pipes, ClientSession, loop buffers).
    gc.collect()
    # Владение снимаем последним: всё выше его требует.
    reset_owner(owner_reset)
    if cancelled is not None:
        raise cancelled


def _error_fact(exc: BaseException | None) -> dict[str, str] | None:
    """Класс и замаскированная причина ошибки для записи итога хода."""
    if exc is None:
        return None
    return {
        "type": exc.__class__.__name__,
        "message": describe_turn_error(exc),
    }


def undelivered_as_arun_args(messages: list[dict]) -> tuple[list[str], dict]:
    """Недоставленные сообщения → аргументы `arun` для отдельного хода.

    Владелец доигрывает хвост (`take_undelivered_messages`) новым ходом, а
    `arun` принимает тексты и параллельные списки полей, тогда как в очередь
    кладут готовые словари. Поля переносятся те, из которых `arun` и строит
    сообщение: id (по нему TUI точечно откатывает реплику), display_content,
    skill_loaded и attachments (картинки из кармана процесса — в тред их
    переложит ход, который их понесёт) — других в очередь не кладут ни
    сервер, ни TUI, ни SDK.
    """
    texts = [str(m.get("content", "")) for m in messages]
    return texts, {
        "message_ids": [m.get("id") for m in messages],
        "display_contents": [m.get("display_content") for m in messages],
        "skills_loaded": [m.get("skill_loaded") for m in messages],
        "attachments": [attachments_of(m) or None for m in messages],
    }


def _put_attachments(msg: dict, attachments, i: int, commit) -> None:
    """Вложения ``i``-го сообщения прогона — в ``msg["attachments"]`` словарями: каждое
    проходит через ``commit`` (копия в тред прогона), ``commit=None`` — как есть, в
    кармане. Нет вложений — ключа нет."""
    atts = attachments[i] if attachments and i < len(attachments) else None
    if atts:
        msg["attachments"] = [
            (commit(a) if commit is not None else a).to_dict() for a in atts
        ]


def _require_plan_execution_mode(mode: str) -> str:
    """План исполняется только в режиме исполнения (edit, auto, manual).

    Возвращает нормализованное имя режима (``"Auto"`` → ``"auto"``)."""
    resolved = coerce_mode(mode)
    if resolved not in PLAN_EXECUTION_MODES:
        raise ValueError(f"not a plan execution mode: {mode!r}")
    return resolved.value


def _require_supported_persist_mode(settings: Any, ctx: Any) -> None:
    """``persist: turn`` — только у durable-исполнителя (Temporal).

    Без истории durable-исполнителя несохранённые шаги пропали бы при падении
    процесса, а UI не видел бы хода."""
    if settings.thread_store.persist == "turn" and not getattr(
        ctx, "durable_execution", False
    ):
        raise ValueError(
            "thread_store.persist: turn is supported only by a durable "
            "executor (Temporal); local runs use persist: step"
        )


def _extend_with_thread_history(
    state: Any, thread_session: Any, backend_api_mode: str
) -> None:
    """chat_completions: история треда — копиями сообщений в ``state.messages``."""
    if (
        thread_session is not None
        and backend_api_mode == "chat_completions"
        and thread_session.history_messages
    ):
        state.messages.extend(
            [
                _shallow_copy_msg(m)
                for m in thread_session.history_messages
                if isinstance(m, dict)
            ]
        )


class AgentExecutor:
    """
    Потокобезопасный запуск агента с возможностью добавлять сообщения во время run
    и останавливать выполнение через stop_run().
    """

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx
        lock_factory = getattr(ctx, "lock_factory", None) or threading.Lock
        self._lock = lock_factory()
        self._pending_messages: list[dict] = []
        #: То, что прогон не успел съесть, снятое с очереди на выходе из цикла.
        #: Держим отдельно, а не выбрасываем: клиенту на эти сообщения уже
        #: ответили «принято», и молча терять их нельзя. Забирает владелец —
        #: `take_undelivered_messages`, вместе с пришедшим после.
        self._undelivered_messages: list[dict] = []
        self._state: State | None = None
        # Сессия последнего arun. Её мету пишет aclose_run_accounting, один раз.
        self._thread_session: Any = None
        self._accounting_closed: bool = False
        # stop_run() до создания state (run() ещё в setup-фазе): запоминаем, и
        # run() применит флаг сразу при создании state, до первого persist.
        self._stop_pending: bool = False
        # Явный выбор режима разрешений (хоткей фронтенда, флаг запуска). Живёт
        # весь срок исполнителя и ложится на состояние каждого прогона: выбор
        # человека сильнее и сохранённого в сессии, и конфига.
        self._permission_mode_choice: Optional[str] = None
        # Отложенный переход статуса плана (одобрение из фронтенда):
        # (статус, режим, тред). Применяется в начале следующего прогона и
        # только к своему треду — см. _apply_plan_changes.
        self._plan_transition_pending: Optional[
            tuple[str, Optional[str], Optional[str]]
        ] = None
        # Человек нажал «Доработать план» в этом треде: статус сменится, когда
        # он реально отправит сообщение.
        self._plan_rework_thread: Optional[str] = None
        self.last_terminal_outcome: Optional[str] = None

        from gc_core.user_stop import UserStop

        self.user_stop = UserStop()

        self.last_closing_reply: str = ""

    def take_undelivered_messages(self) -> list[dict]:
        """Забрать всё, что отдано прогону, но до агента не дошло, — в порядке прихода.

        Это хвост, снятый с очереди на выходе из цикла, и то, что легло в
        очередь уже после: цикл вычерпывает её в последний раз при закрытии
        хода, а прогон ещё сохраняет тред и сносит MCP-сессии — через `await`,
        и не быстро. Звать ПОСЛЕ `arun`: из идущего прогона это украло бы
        очередь у цикла.

        Владелец (сервер, TUI) доигрывает хвост отдельным ходом того же треда
        (`undelivered_as_arun_args`) — пока он этого не сделал, прогон нельзя
        объявлять законченным. Если не забрать, пришедшее после `finally`
        уедет следующему прогону вслед за его собственным запросом, а снятое
        `finally` так и останется лежать.
        """
        with self._lock:
            out = [*self._undelivered_messages, *self._pending_messages]
            self._undelivered_messages = []
            self._pending_messages = []
            return out

    def _take_pending_messages(self) -> list[dict]:
        with self._lock:
            out = self._pending_messages[:]
            self._pending_messages.clear()
            state = self._state
        # Сообщение из очереди — такое же новое слово человека, как
        # стартовое: снимает паузу судьи и посреди прогона.
        if state is not None:
            self._lift_judge_pause(state, out)
        # Вне замка: копия картинок в тред — файловая операция, а замок ждут
        # поток TUI и обработчик сервера.
        return self._commit_queued(out)

    def _is_stop_requested(self) -> bool:
        with self._lock:
            return (
                self._state.stop_requested
                if self._state is not None
                else False
            )

    async def _alist_skills(self):
        loader = port(self._ctx, "skills_loader")
        if loader is None:
            loader = self._ctx.skills_registry.list_skills
        return await loader()

    async def _aopen_thread_session(
        self,
        thread_id,
        *,
        start_new_thread: bool,
        system,
        backend_api_mode: str,
        logger,
    ):
        """Тред-сессия прогона → (thread_session, thread_shared, previous_response_id, system_id)."""
        system_id = system or "cli"
        if thread_id is None:
            clear_thread_identity(self._ctx)
            return None, None, None, system_id
        if start_new_thread:
            # Сессии ещё нет, поэтому хранилище берём из контекста; нет и
            # там — файловое, как раньше.
            _reset_store = getattr(self._ctx, "thread_store", None)
            if _reset_store is None:
                from gc_core.thread_store_file import FileThreadStore

                _reset_store = FileThreadStore(resolve_threads_dir(self._ctx))
            from gc_core.thread_store_async import athread_store

            await athread_store(_reset_store).areset(system_id, thread_id)
            # Снимки баз — не забота хранилища: тем же ``reset`` ходит
            # запасной путь компакции, где сносить базы отката нельзя.
            # Пользовательский сброс знает, что чистит, и чистит здесь.
            # Вне цикла событий: снимков у долгого треда тысячи, и снос
            # каталога прямо в корутине подвешивал бы старт нового треда.
            await asyncio.to_thread(
                purge_thread_snapshots,
                resolve_threads_dir(self._ctx),
                system_id,
                thread_id,
                launch_root=launch_root_from_config(self._ctx),
            )
        thread_session = await ThreadSession.afrom_config(
            settings=self._ctx,
            system_id=system_id,
            thread_id=thread_id,
            # Хранилище встраивающего, если он его передал. None — файловое
            # по умолчанию, ровно прежнее поведение.
            store=getattr(self._ctx, "thread_store", None),
        )
        if logger is not None:
            # Единственная связка «этот лог-файл ↔ этот тред»: на момент
            # создания логгера тред ещё не разрешён, поэтому в agent_start
            # его нет, а RunContextFilter ставит только run_id. Без этой
            # записи тред виден в логе лишь косвенно — через сообщения
            # суммаризации, и только если она вообще срабатывала.
            #
            # Значения продублированы в тексте сообщения намеренно: поля из
            # extra= рендерит только JsonlFormatter, а при logging.format:
            # text (дефолт) в строке осталось бы голое имя события.
            logger.info(
                "thread_bound thread_id=%s system_id=%s",
                thread_session.thread_id,
                thread_session.system_id,
                extra={
                    "event": "thread_bound",
                    "thread_id": thread_session.thread_id,
                    "system_id": thread_session.system_id,
                },
            )
        previous_response_id = None
        if backend_api_mode == "chat_completions":
            thread_session.history_messages = repair_tool_call_pairing(
                thread_session.history_messages,
                keep_latest_unanswered=False,
            )
        if backend_api_mode == "responses":
            previous_response_id = thread_session.previous_response_id
        meta = getattr(thread_session, "meta", None) or {}
        thread_shared = ThreadMetaMapper.shared_from_meta(meta)
        # Sidecar nested-trace: identity для child ctx (независимо от TUI sink).
        bind_thread_identity(
            self._ctx,
            threads_dir=thread_session.threads_dir,
            system_id=thread_session.system_id,
            thread_id=thread_session.thread_id,
        )
        return thread_session, thread_shared, previous_response_id, system_id

    async def _amcp_snapshot(
        self, thread_session, thread_id, system_id: str
    ) -> None:
        # Снимок MCP «сервер→тулы» для TUI-экрана /mcp (необязателен — не валим run).
        if thread_id is None or not settings_of(self._ctx).mcp.servers:
            return
        try:
            from gc_core.tools.mcp_snapshot import build_mcp_tools_snapshot

            snap = build_mcp_tools_snapshot(
                self._ctx, self._ctx.tools_data, self._ctx.mcp_status
            )
            await thread_session.port.aupdate_runtime_meta(
                system_id,
                thread_id,
                {"mcp_tools": snap},
            )
        except Exception:
            pass

    async def _aload_chat_completions_prompts(
        self, *, effective_verbose: bool, logger
    ) -> tuple:
        """(system prompt, special instructions) для chat/completions из файлов конфига.

        Без `prompt_loader` в контексте файлы читаются здесь же, как в develop;
        с ним (Temporal native) чтение уходит в загрузчик рантайма.
        """
        from gc_core.agent_store import agent_store_of
        from gc_core.run_preparation import (
            PromptPreparationRequest,
            load_run_prompts,
        )

        request = PromptPreparationRequest.from_config(
            self._ctx, verbose=effective_verbose
        )
        loader = port(self._ctx, "prompt_loader")
        if loader is None:
            prepared = load_run_prompts(
                request, logger=logger, store=agent_store_of(self._ctx)
            )
        else:
            prepared = await loader(request)
        return prepared.system_prompt, prepared.special_instructions

    async def _abuild_run_state(
        self,
        *,
        effective_verbose,
        skills_list,
        chat_completions_system_prompt,
        special_instructions,
        thread_id,
        thread_shared,
        thread_session,
        backend_api_mode: str,
        logger,
    ) -> tuple:
        """State прогона и отрендеренный system prompt (chat/completions) либо None."""
        settings = settings_of(self._ctx)
        state_data = {
            "limit_project_metadata": settings.agent.limit_project_metadata,
            "parent_state_id": None,
            "verbose": effective_verbose,
            "model": settings.agent.model,
            "tools_spec": self._ctx.tools_spec,
            "tools_descr": self._ctx.tools_description,
            "max_steps": settings.agent.max_steps,
            "skills_list": skills_list,
            "system_prompt_id": settings.agent.system_prompt_id,
            "chat_completions_system_prompt": chat_completions_system_prompt,
            "use_server_subagents": settings.agent.use_server_subagents,
            "agent_md_path": settings.paths.agent_md_path,
            "is_tui": bool(getattr(self._ctx, "is_tui", False)),
            "agentsmd_override": settings.tui.agentsmd_override,
            "thread_id": thread_id,
            "shared": thread_shared,
            "workspace_config": self._ctx,
            "logger": logger,
            "workspace": getattr(self._ctx, "workspace", None),
        }
        if subagents_enabled(settings.agent.use_local_subagents):
            state_data["specific_role"] = ORCHESTRATOR_SPECIFIC_ROLE
        preparer = port(self._ctx, "state_preparer")
        if preparer is None:
            state = await State.acreate(**state_data)
        else:
            from gc_core.state_preparation import StatePreparationRequest

            prepared = await preparer(
                StatePreparationRequest(
                    agent_md_path=state_data["agent_md_path"],
                    is_tui=state_data["is_tui"],
                    agentsmd_override=state_data["agentsmd_override"],
                    verbose=effective_verbose,
                )
            )
            state = State.from_prepared(prepared, **state_data)
        attach = getattr(self._ctx, "attach_state", None)
        if callable(attach):
            attach(state)
        # Граница хода сбрасывает счётчики; модель выбирается перед вызовом.
        begin_turn_main_trace(state, self._ctx)
        # Выбранное до старта уже показано фронтендом: применённый выбор
        # рана — текущий, без события.
        router = self.get_model_router()
        if router is not None:
            state.applied_choice = router.live_choice()
        state.tool_call_outputs_store.set_max_entries(
            get_stored_tool_outputs_max_entries(settings)
        )
        state.backend_api_mode = backend_api_mode
        state.on_messages_evicted = getattr(
            self._ctx, "on_messages_evicted", None
        )
        state.dialog_steps_offset = 0
        if thread_session is not None:
            meta = getattr(thread_session, "meta", None) or {}
            state.init_user_intent_log(meta.get("user_intent_log"))
            state.init_current_user_intent(meta.get("current_user_intent"))
            state.init_user_turn_seq(meta.get("user_turn_seq"))
            state.infer_user_turn_seq_if_missing()
            state.init_tool_usage_notice_from_meta(meta)
            state.dialog_steps_offset = int(
                meta.get("dialog_steps_offset", 0) or 0
            )
            (
                state.last_main_prompt_tokens,
                state.last_main_prompt_message_count,
            ) = ThreadMetaMapper.read_prompt_token_fields(meta)
            state.usage = UsageAccount.from_meta(meta.get("token_usage"))
        rendered_system_prompt: str | None = None
        if (
            backend_api_mode == "chat_completions"
            and isinstance(chat_completions_system_prompt, str)
            and chat_completions_system_prompt.strip()
        ):
            # Для chat/completions backend'а system prompt должен быть в истории сообщений.
            # Делаем это на уровне state-сессии, чтобы transport/formatting не знал
            # про источники/файлы системного промпта.
            rendered_system_prompt = render_system_prompt(
                system_prompt=chat_completions_system_prompt.strip(),
                tools_spec=state.tools,
                project_info=state.initial_project_data,
                specific_role=state.specific_role,
                special_instructions=special_instructions,
            )
            state.messages = [
                {
                    "role": "system",
                    "content": rendered_system_prompt,
                    "id": generate_message_id_func("m_sys"),
                }
            ]
        return state, rendered_system_prompt

    def _bind_state(self, state, *, discard_writes_after_stop: bool) -> None:
        with self._lock:
            self._state = state
            self._state.discard_writes_after_stop = bool(
                discard_writes_after_stop
            )
            # Применить stop, пришедший пока state ещё не существовал (setup-фаза),
            # чтобы первый же check_stop вышел ДО persist user-сообщения.
            if self._stop_pending:
                self._state.stop_requested = True
                self._stop_pending = False
                # Стоп лёг на ход только сейчас: часы этого хода, открытые
                # в сборке состояния, закрываются этим моментом.
                clock = current_turn_clock(self._ctx)
                if clock is not None:
                    clock.close()
            # В shared уже лежит выбор, восстановленный из сессии; явный выбор
            # этого запуска — поверх. Под тем же замком, что публикует state:
            # иначе между публикацией и наложением вклинился бы
            # set_permission_mode и его значение было бы затёрто.
            if self._permission_mode_choice is not None:
                self._state.shared.permission_mode = (
                    self._permission_mode_choice
                )
            # Очередь здесь НЕ сбрасываем. Сброс стоял тут с тех пор, когда до
            # него не было setup-фазы и класть сообщения заранее было некому.
            # Теперь кладут: сервер, пока прогон ещё не дошёл до цикла,
            # `_prebuild_messages` в TUI, `AsyncAgent.send` — и всё это
            # стиралось, не доходя до агента. Пустой очередь между прогонами
            # НЕ бывает по построению: сообщение может лечь в неё и после
            # последнего забора циклом — пока прогон сохраняет тред и сносит
            # MCP. Такой хвост владелец забирает `take_undelivered_messages` и
            # доигрывает отдельным ходом. Не забранное из очереди уходит этому
            # прогону вслед за его собственными сообщениями (см. ниже), а
            # отложенное `finally` прошлого прогона ждёт владельца.

    def accept_plan(self, mode: str, thread_id: Optional[str]) -> None:
        """Человек принял план и выбрал режим исполнения.

        Фронтенд зовёт это ДО запуска продолжения; статус плана сменится в
        начале следующего прогона. Переход одноразовый и привязан к треду:
        если продолжение не стартовало, он не должен примениться к прогону
        другой сессии.
        """
        with self._lock:
            self._plan_transition_pending = (
                PLAN_STATUS_APPROVED,
                mode,
                thread_id,
            )

    def arm_plan_rework(self, thread_id: Optional[str]) -> None:
        """Человек выбрал «Доработать план»: следующее его сообщение в этом
        треде переведёт план в доработку."""
        with self._lock:
            self._plan_rework_thread = thread_id

    def prepare_accepted_plan(
        self, mode: str, thread_id: Optional[str], *, extra_text: str = ""
    ) -> list[str]:
        """Человек одобрил предложенный план: режим исполнения, одобрение и
        сообщения продолжения — без прогона.

        Для фронтенда, который ведёт прогон своим путём (ACP — потоком дельт
        ``AsyncAgent.stream``); остальные зовут ``aexecute_accepted_plan``.
        Не режим исполнения — ``ValueError`` до любых изменений.
        ``extra_text`` — уточнение человека к исполнению, отдельным
        сообщением после служебного «план одобрен».
        """
        mode = _require_plan_execution_mode(mode)
        self.set_permission_mode(mode)
        self.accept_plan(mode, thread_id)
        return accepted_plan_messages(mode, extra_text=extra_text)

    async def aexecute_accepted_plan(
        self,
        mode: str,
        thread_id: Optional[str],
        *,
        extra_text: str = "",
        verbose: Optional[bool] = None,
        account_run: bool = True,
        system: Optional[str] = None,
    ) -> Any:
        """Человек одобрил предложенный план: исполнить его в выбранном режиме.

        Одна последовательность на фронтенды — консольный вопрос и
        ``--approve-plan`` gc-cli (ACP — через ``prepare_accepted_plan``).
        Негодный режим — ``ValueError`` до любых действий, и учёт прошлого
        прогона тоже не трогается. Учёт закрывается ДО смены режима: его
        мета пишется такой, какой была на его конце; закрытый учёт и
        прогон, которого в этом процессе не было, — no-op.
        """
        _require_plan_execution_mode(mode)
        await self.aclose_run_accounting()
        messages = self.prepare_accepted_plan(
            mode, thread_id, extra_text=extra_text
        )
        return await self.arun(
            messages,
            thread_id=thread_id,
            system=system,
            verbose=verbose,
            account_run=account_run,
        )

    def plan_snapshot(
        self, expected_thread_id: Optional[str]
    ) -> Optional[dict]:
        """План указанного треда из живого состояния; None — другой тред,
        состояния нет или плана нет. Пустой id — всегда несовпадение."""
        with self._lock:
            state = self._state
            if (
                state is None
                or not expected_thread_id
                or getattr(state, "thread_id", None) != expected_thread_id
            ):
                return None
            return normalize_plan_block(getattr(state.shared, "plan", None))

    def _apply_plan_changes(self, state, new_user_messages) -> None:
        """Применить к плану то, что человек решил между прогонами.

        Оба намерения гасятся в начале ЛЮБОГО прогона и действуют только на
        свой тред: невидимое взведённое состояние не должно пережить момент,
        когда его создали, и выстрелить в чужой сессии.
        """
        with self._lock:
            pending = self._plan_transition_pending
            rework_thread = self._plan_rework_thread
            self._plan_transition_pending = None
            self._plan_rework_thread = None
        thread_id = getattr(state, "thread_id", None)
        plan = getattr(state.shared, "plan", None)
        if not thread_id or not isinstance(plan, dict):
            return
        if pending is not None and pending[2] == thread_id:
            status, approved_mode, _thread = pending
            plan["status"] = status
            plan["approved_mode"] = approved_mode
            plan["updated_at"] = now_stamp()
            # Выход из plan через одобрение: «план не одобрен» слать нельзя
            # — продолжение несёт своё «план одобрен».
            if (
                status == PLAN_STATUS_APPROVED
                and state.mode_announced == Mode.PLAN.value
            ):
                state.mode_announced = None
            return
        # «В доработке» — только по двум шагам сразу: человек нажал «Доработать»
        # И отправил обычное сообщение, оставаясь в plan. Иначе вопрос не по
        # теме после закрытого баннера уводил бы план в доработку.
        first = new_user_messages[0] if new_user_messages else None
        if (
            rework_thread == thread_id
            and first is not None
            and extract_real_user_message_text(first)
            and resolve_mode(self._ctx) is Mode.PLAN
        ):
            plan["status"] = PLAN_STATUS_REWORKING
            plan["approved_mode"] = None
            plan["updated_at"] = now_stamp()

    def _export_plan_file(self, state, logger) -> None:
        """Файл плана для человека — раз в конце прогона, рядом с записью меты.

        Пишет хранилище, если умеет (файловый стор); сбой экспорта прогон не
        роняет: файл — удобство, канон плана уже сохранён в мете.
        """
        session = self._thread_session
        save = getattr(getattr(session, "store", None), "save_plan_file", None)
        plan = getattr(state.shared, "plan", None)
        if not callable(save) or not plan:
            return
        try:
            save(session.system_id, session.thread_id, plan)
        except Exception as exc:
            if logger is not None:
                logger.warning("plan file export failed: %s", exc)

    def clear_plan(self, thread_id: Optional[str]) -> None:
        """Человек убрал план этой сессии: взведённые решения по нему (утвердить,
        доработать) больше не к чему применять."""
        with self._lock:
            pending = self._plan_transition_pending
            if pending is not None and pending[2] == thread_id:
                self._plan_transition_pending = None
            if self._plan_rework_thread == thread_id:
                self._plan_rework_thread = None

    @staticmethod
    def _lift_judge_pause(state, new_user_messages) -> None:
        """Новое сообщение человека снимает паузу судьи
        (`lift_pause_on_user_message`). Служебные сообщения человеком не
        считаются."""
        if any(extract_real_user_message_text(m) for m in new_user_messages):
            lift_pause_on_user_message(state.shared)

    def _plan_notices(self, state) -> list[str]:
        """Служебное сообщение в начале прогона, один раз: «план убран
        человеком». О режиме агенту говорит цикл перед каждым запросом к
        модели (``processing_responses._mode_notices``)."""
        shared = state.shared
        if not shared.plan_removed_notice:
            return []
        shared.plan_removed_notice = False
        return [PLAN_REMOVED_MESSAGE]

    @staticmethod
    def _seed_current_user_intent(state, new_user_messages) -> None:
        # На старте user turn дописываем текущий CLI-запрос в current_user_intent
        # (только реальные user-сообщения, не technical). user_intent_log — только
        # user_intent_log — только после flush при accept turn_finish.
        for new_user_message in new_user_messages:
            state.finish_gate_rejection_count = 0
            text = extract_real_user_message_text(new_user_message)
            if text:
                state.current_user_intent = append_user_ask_deduped(
                    state.current_user_intent,
                    text,
                    user_turn_seq=state.user_turn_seq,
                )

    def _thread_persist_callback(
        self,
        *,
        thread_session,
        state,
        backend_api_mode: str,
        rendered_system_prompt,
    ):
        async def _persist_thread_batch(step_messages: list[dict]) -> None:
            if is_persist_discarded(state):
                return
            # Persist notes from in-memory state into thread meta.
            # Tools mutate state, but ThreadSession writes self.meta to disk.
            if (
                backend_api_mode == "chat_completions"
                and isinstance(rendered_system_prompt, str)
                and rendered_system_prompt.strip()
            ):
                thread_session.meta[
                    "chat_completions_system_prompt_rendered"
                ] = rendered_system_prompt
            _shared = getattr(state, "shared", None)
            if isinstance(_shared, SharedStateData):
                thread_session.meta = ThreadMetaMapper.merge_shared_into_meta(
                    thread_session.meta, _shared
                )
            known_before = set(thread_session.known_message_keys)
            step_messages_for_user_scope = [
                m
                for m in step_messages
                if make_message_key(m) not in known_before
            ]
            state.current_user_intent = append_user_asks_from_messages(
                state.current_user_intent,
                step_messages_for_user_scope,
                user_turn_seq=state.user_turn_seq,
            )
            thread_session.meta = ThreadMetaMapper.merge_state_into_meta(
                thread_session.meta, state
            )
            thread_session.meta["backend_api_mode"] = state.backend_api_mode
            thread_session.backend_api_mode = state.backend_api_mode
            persist_tool_usage_notice_state(state, thread_session.meta)
            last_backend_response_id = getattr(
                state, "last_backend_response_id", None
            )
            if (
                isinstance(last_backend_response_id, str)
                and last_backend_response_id
            ):
                thread_session.meta["last_backend_response_id"] = (
                    last_backend_response_id
                )
            await thread_session.apersist_batch(
                step_messages, previous_response_id=state.previous_response_id
            )

        return _persist_thread_batch

    def _persistence_for_run(
        self,
        *,
        thread_session,
        state,
        backend_api_mode: str,
        rendered_system_prompt,
        previous_response_id,
        new_user_messages,
        summary_trigger_chars: int,
        summary_trigger_tokens: int,
    ) -> tuple:
        """(messages для run_agent, persist_messages_callback, get_full_history_messages)."""
        messages = list(new_user_messages)
        if thread_session is not None:
            state.previous_response_id = previous_response_id
            thread_session.set_runtime_messages(state.messages)

            # chat/completions: история уже в state.messages (extend выше).
            # В run_agent передаём только новый user-батч — backend собирает payload
            # из state + batch без дублей (см. create_call_data_completions).
            if backend_api_mode == "chat_completions":
                base_messages = list(thread_session.history_messages)
                state.skip_initial_user_ask_step_when_history = (
                    len(base_messages) > 0
                )
                messages = list(new_user_messages)
            else:
                base_messages = (
                    list(thread_session.history_messages)
                    if previous_response_id is None
                    else []
                )
                state.skip_initial_user_ask_step_when_history = (
                    len(base_messages) > 0
                )
                messages = [*base_messages, *new_user_messages]
            persist = self._thread_persist_callback(
                thread_session=thread_session,
                state=state,
                backend_api_mode=backend_api_mode,
                rendered_system_prompt=rendered_system_prompt,
            )
            return messages, persist, thread_session.get_full_history_messages
        if backend_api_mode == "chat_completions" and (
            summary_trigger_chars > 0 or summary_trigger_tokens > 0
        ):

            def _persist_runtime_batch(step_messages: list[dict]) -> None:
                if is_persist_discarded(state):
                    return
                state.current_user_intent = append_user_asks_from_messages(
                    state.current_user_intent,
                    step_messages,
                    user_turn_seq=state.user_turn_seq,
                )

            return messages, _persist_runtime_batch, None
        return messages, None, None

    async def _afinalize_thread(self, thread_session, state, logger) -> None:
        if thread_session is None or (
            state.discard_writes_after_stop and state.stop_requested
        ):
            return
        thread_session.meta = ThreadMetaMapper.merge_state_into_meta(
            thread_session.meta, state
        )
        thread_session.meta["backend_api_mode"] = state.backend_api_mode
        persist_tool_usage_notice_state(state, thread_session.meta)
        await thread_session.apersist_batch(
            [], previous_response_id=state.previous_response_id
        )
        # persist: turn — конец хода и есть точка сброса.
        flush = getattr(thread_session, "aflush_deferred", None)
        if flush is not None:
            await flush()
        _run_trace_id = trace_id_from_config(self._ctx)
        if _run_trace_id:
            # После persist_batch: он пересобирает мету с диска, поэтому
            # запись должна быть последней, иначе её затрёт.
            await _aappend_trace_id_to_meta(
                thread_session, _run_trace_id, logger
            )

    def _report_run_usage(
        self, state, run_start_usage, logger, *, console: bool = True
    ) -> None:
        # Отчёт по токенам/стоимости за этот прогон (дельта от снимка до старта).
        # ``console=False`` — тот же отчёт только в лог, без таблицы в stdout.
        settings = settings_of(self._ctx)
        try:
            run_usage = state.usage.delta_from(run_start_usage)
            # Доступно in-memory: get_state().usage (кумулятив) и .last_run_usage (этот прогон).
            state.last_run_usage = run_usage
            usage_text = build_run_usage_report(
                run_usage,
                settings.pricing,
                settings.agent.model,
                settings.backend.completions_url,
                provider_currency=getattr(
                    self._ctx, "provider_currency", None
                ),
            )
            if usage_text:
                if logger is not None:
                    logger.info(
                        "\n<run_usage>\n%s\n</run_usage>\n", usage_text
                    )
                if console and human_console_enabled(settings):
                    print(f"\n{usage_text}\n")
            if logger is not None:
                usage_dict = build_run_usage_dict(
                    run_usage,
                    settings.pricing,
                    settings.agent.model,
                    settings.backend.completions_url,
                    provider_currency=getattr(
                        self._ctx, "provider_currency", None
                    ),
                )
                if usage_dict:
                    logger.info(
                        "run_usage",
                        extra={"event": "run_usage", "usage": usage_dict},
                    )
        except Exception as usage_exc:  # отчёт не должен ронять прогон
            if logger is not None:
                logger.warning(
                    "run usage report failed: %s", usage_exc, exc_info=True
                )

    async def aclose_run_accounting(self, *, console: bool = True) -> None:
        """Закрыть прогон: один раз записать мету треда и отчёт usage.

        К этому моменту цикл и все шаги после него уже прошли. Повторный
        вызов ничего не пишет. ``arun`` зовёт метод сам. Кто после цикла ещё
        работает, передаёт ``account_run=False`` и зовёт его после своих шагов.

        Ошибка записи меты уходит вызывающему: прогон без сохранённого
        состояния не выглядит успешным. Сбой отчёта usage только пишется
        в лог.
        """
        if self._accounting_closed:
            return
        state = self.get_state()
        if state is None:
            return
        logger = getattr(self._ctx, "logger", None)
        await self._afinalize_thread(self._thread_session, state, logger)
        self._accounting_closed = True
        self._export_plan_file(state, logger)
        try:
            baseline = UsageAccount.from_meta(
                getattr(state, "run_start_usage_meta", None)
            )
            self._report_run_usage(state, baseline, logger, console=console)
        except Exception as usage_exc:
            if logger is not None:
                logger.warning(
                    "run usage report failed: %s",
                    usage_exc,
                    exc_info=True,
                )

    async def _apersist_finished_loop_during_cancel(self) -> None:
        """Записать учёт уже завершённого цикла, хотя уборку MCP сняли.

        В снятой задаче следующий ``await`` сразу получает ``CancelledError``,
        и запись не начинается. Снимаем отмену и пишем. ``arun`` затем бросает
        ``CancelledError`` заново — вызывающий её видит. Счётчик не
        восстанавливаем: иначе задача останется снятой даже после того,
        как вызывающий эту ошибку поймает.
        """
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
        await self.aclose_run_accounting()

    async def _arecord_model_choice(
        self, record: Optional[ModelChoiceRecord]
    ) -> None:
        """Записать смену модели в тред за последним записанным сообщением.

        Зовут подготовка вызова (через `RunContext.record_model_choice`) и
        завершение рана. Сбой записи — предупреждение в журнал прогона, ран
        он не роняет."""
        session = self._thread_session
        state = self.get_state()
        if record is None or session is None or state is None:
            return
        try:
            await session.aappend_model_choice(
                record, discarded=is_persist_discarded(state)
            )
        except Exception:
            logger = getattr(self._ctx, "logger", None)
            if logger is not None:
                logger.warning("model_choice_write_failed", exc_info=True)

    async def _ateardown_run(self, owner_reset: Any) -> BaseException | None:
        # Keep the unconsumed queue before the await; messages arriving during
        # MCP cleanup remain pending and are returned by take_undelivered_messages.
        with self._lock:
            self._undelivered_messages.extend(self._pending_messages)
            self._pending_messages = []
        try:
            cleanup = port(self._ctx, "mcp_cleanup")
            if cleanup is None:
                await _aclose_mcp_and_release(owner_reset)
            else:
                try:
                    await cleanup()
                except Exception:
                    # Как локальная уборка: сбой закрытия MCP не отменяет
                    # готовый ответ хода.
                    logger = getattr(self._ctx, "logger", None)
                    if logger is not None:
                        logger.warning("mcp_cleanup_failed", exc_info=True)
                finally:
                    reset_owner(owner_reset)
        except asyncio.CancelledError as exc:
            # arun persists accounting for a completed loop before re-raising.
            return exc
        return None

    def _report_log_path(self, logger: Any) -> None:
        if human_console_enabled(self._ctx):
            print(f"\nПуть к файлу с логами: {self._ctx.logging_path}\n")
        if logger is not None:
            logger.info(
                "log_path",
                extra={
                    "event": "log_path",
                    "path": getattr(self._ctx, "logging_path", None),
                },
            )

    async def arun(
        self,
        messages: list[str],
        *,
        thread_id: str | None = None,
        start_new_thread: bool = False,
        system: str | None = None,
        verbose: bool | None = None,
        discard_writes_after_stop: bool = False,
        message_ids: list[str] | None = None,
        display_contents: list[str | None] | None = None,
        skills_loaded: list[str | None] | None = None,
        attachments: list[list[Attachment] | None] | None = None,
        account_run: bool = True,
    ) -> Any:
        """Прогон агента, не блокирующий вызывающий loop.

        При каждом вызове state создаётся заново. messages — непустой список
        текстов, каждый уходит отдельным user-сообщением.

        attachments — вложения по сообщениям, параллельно messages (как
        display_contents): элемент — картинки своего сообщения из кармана
        процесса (``ImageStore.stage``) или None. В тред их копирует сам ход
        (``commit_to_run_thread``); текст сообщения остаётся строкой с
        плашками ``[Изображение #N]``.

        Форма ОДНА. Синхронного ``run`` нет: из синхронного кода агента
        ведут своим ``asyncio.run`` в рабочем потоке — так делают TUI и
        harbor-адаптер.

        ``account_run=False`` — не закрывать учёт внутри. Вызывающий ещё
        добавит шаги после цикла и сам вызовет ``aclose_run_accounting``.
        Отмена закрытия MCP после уже завершённого цикла учёт всё равно
        записывает и затем пробрасывается: дополнительного шага уже не будет.
        """
        with self._lock:
            # State прошлого хода снимаем ПЕРВЫМ делом. Пока он жил до конца
            # объекта, `stop_run` в сетапе этого хода (чтение треда, сборка
            # состояния, остановка uvicorn) писал флаг в мёртвый state: новый
            # стартовал чистым, и ход доигрывался целиком — с вызовами модели и
            # тулов, уже после нажатия стопа. Второй ход у владельца исполнителя
            # обычное дело: хвост прогона у сервера и SDK, следующий запрос в
            # TUI. Теперь такой стоп ложится в `_stop_pending` и применяется при
            # создании state (ниже по функции).
            #
            # Именно в НАЧАЛЕ хода, а не в конце прошлого: стоп по остывшему
            # прогону (ход кончился, ничего не идёт) обязан пропасть, иначе он
            # молча погасил бы следующий запрос человека.
            self._state = None
            self._accounting_closed = False
        settings = settings_of(self._ctx)
        # Раньше стопа: неверный persist не должен падать на отсутствии
        # ``user_stop`` и не вооружает снятие хода, который не начнётся.
        _require_supported_persist_mode(settings, self._ctx)
        self.user_stop.prepare_run(
            asyncio.get_running_loop(), getattr(self._ctx, "logger", None)
        )
        logger = getattr(self._ctx, "logger", None)
        effective_verbose = (
            verbose if verbose is not None else settings.agent.verbose
        )
        skills_list = await self._alist_skills()
        backend_api_mode = str(settings.backend.api_mode).strip().lower()
        (
            thread_session,
            thread_shared,
            previous_response_id,
            system_id,
        ) = await self._aopen_thread_session(
            thread_id,
            start_new_thread=start_new_thread,
            system=system,
            backend_api_mode=backend_api_mode,
            logger=logger,
        )
        self._thread_session = thread_session
        # Turn identity: свежий id на каждый run(), независимо от треда — без
        # него нечем будет пометить записи журнала правок текущего хода.
        bind_turn_identity(self._ctx, mint_turn_id())
        is_cli_mode = system_id == "cli"

        # Снять протухшие runtime-индикаторы одним атомарным store batch,
        # сохраняя прежний путь у пользовательских хранилищ без capability.
        await areset_run_flags(thread_session)
        await self._amcp_snapshot(thread_session, thread_id, system_id)

        # Начальные значения для координатора; проверка истории перечитывает
        # пороги подготовленного вызова через limits_provider.
        limits_provider = self._history_summary_limits
        initial_limits = limits_provider()
        summary_trigger_chars = initial_limits.chars_during
        summary_trigger_tokens = initial_limits.tokens_during
        turn_end_trigger_chars = initial_limits.chars_after
        turn_end_trigger_tokens = initial_limits.tokens_after

        chat_completions_system_prompt: str | None = None
        special_instructions: str | None = None
        if backend_api_mode == "chat_completions":
            (
                chat_completions_system_prompt,
                special_instructions,
            ) = await self._aload_chat_completions_prompts(
                effective_verbose=effective_verbose,
                logger=logger,
            )

        state, rendered_system_prompt = await self._abuild_run_state(
            effective_verbose=effective_verbose,
            skills_list=skills_list,
            chat_completions_system_prompt=chat_completions_system_prompt,
            special_instructions=special_instructions,
            thread_id=thread_id,
            thread_shared=thread_shared,
            thread_session=thread_session,
            backend_api_mode=backend_api_mode,
            logger=logger,
        )
        self._bind_state(
            state, discard_writes_after_stop=discard_writes_after_stop
        )

        # Permission memory: store оборачивает state.shared.confirm_rules ПО ССЫЛКЕ —
        # session-правила пишутся прямо в shared и сохраняются обычной персистентностью.
        self._ctx.permission_store = PermissionStore(
            session_rules=state.shared.confirm_rules, shared=state.shared
        )
        _extend_with_thread_history(state, thread_session, backend_api_mode)

        execution_patch = port(self._ctx, "execution_patch")
        deferred_startup_compaction = (
            execution_patch is None
            or execution_patch("startup-compaction-call-kit-v1")
        )
        summary = RunSummaryCoordinator(
            ctx=self._ctx,
            state=state,
            thread_session=thread_session,
            summary_trigger_chars=summary_trigger_chars,
            turn_end_trigger_chars=turn_end_trigger_chars,
            summary_trigger_tokens=summary_trigger_tokens,
            turn_end_trigger_tokens=turn_end_trigger_tokens,
            is_cli_mode=is_cli_mode,
            pre_backend_trigger_reason="pre_backend",
            logger=logger,
            limits_provider=limits_provider,
            deferred_startup_compaction=deferred_startup_compaction,
            startup_progress_guard=(
                deferred_startup_compaction
                and (
                    execution_patch is None
                    or execution_patch("startup-compaction-progress-v1")
                )
            ),
        )
        self._ctx.on_summary_applied = summary.on_summary_applied
        self._ctx.record_model_choice = self._arecord_model_choice
        self._ctx.is_cli_mode = is_cli_mode

        # Старые истории сохраняют порядок команд. Новые исполнения проверяют
        # стартовую историю в цикле после выбора первого комплекта модели.
        if not deferred_startup_compaction:
            await _acall_blocking_hook(
                summary.apre_run_compact_if_needed, trigger_reason="pre_run"
            )

        if not messages:
            raise ValueError(
                "Список пользовательских сообщений не должен быть пустым."
            )
        user_bodies = [str(x) for x in messages]
        # Копия вложений в тред — здесь и при приёме очереди, и только после
        # сброса треда и привязки его адреса к контексту выше: копия, сделанная
        # до сброса, пропала бы вместе с тредом.
        commit_attachment = functools.partial(commit_to_run_thread, self._ctx)
        # id можно задать снаружи (TUI пробрасывает свой, чтобы потом точечно
        # откатить осиротевшую реплику при быстрой отмене); иначе генерим.
        new_user_messages = self._build_user_messages(
            user_bodies,
            message_ids=message_ids,
            display_contents=display_contents,
            skills_loaded=skills_loaded,
            gen_id=generate_message_id_func,
            attachments=attachments,
            commit=commit_attachment,
        )
        self._seed_current_user_intent(state, new_user_messages)
        self._apply_plan_changes(state, new_user_messages)
        self._lift_judge_pause(state, new_user_messages)
        # «План убран» — до сообщений человека: сначала контекст, потом
        # запрос. О режиме цикл скажет перед первым запросом к модели.
        for text in reversed(self._plan_notices(state)):
            new_user_messages.insert(
                0,
                {
                    "role": "user",
                    "content": wrap_technical_user_content(text),
                    "id": generate_message_id_func("m"),
                },
            )

        messages, persist_messages_callback, get_full_history_messages = (
            self._persistence_for_run(
                thread_session=thread_session,
                state=state,
                backend_api_mode=backend_api_mode,
                rendered_system_prompt=rendered_system_prompt,
                previous_response_id=previous_response_id,
                new_user_messages=new_user_messages,
                summary_trigger_chars=summary_trigger_chars,
                summary_trigger_tokens=summary_trigger_tokens,
            )
        )

        # Снимок usage до прогона: для resume треда state.usage гидрирован кумулятивом,
        # отчёт же показывает токены именно этого прогона (дельту). Тот же baseline
        # кладём на state — по нему TUI-футер считает live-«запрос» текущего хода.
        run_start_usage = UsageAccount.from_meta(state.usage.to_meta())
        state.run_start_usage_meta = run_start_usage.to_meta()
        # Входим во владение MCP-сессиями этого контекста. Всё, что поднимут
        # тулы внутри прогона, ляжет под этот токен — и на выходе мы закроем
        # ровно своё, не тронув сессии параллельных агентов на том же loop.
        _owner_reset = set_owner(
            getattr(self._ctx, "mcp_session_owner", "") or None
        )
        loop_finished = False
        teardown_cancel: BaseException | None = None
        run_error: BaseException | None = None
        # Накопленное ДО старта уходит в ПЕРВЫЙ батч, после собственных
        # сообщений прогона: клиент, которому ответили `queued`, должен попасть
        # к модели в том же запросе, а не ждать следующего хода.
        queued_before_loop = self._take_pending_messages()
        if queued_before_loop:
            messages = [*messages, *queued_before_loop]
        from gc_core.user_stop import bind_user_stop

        _stop_bound = bind_user_stop(self.user_stop)
        _stop_bound.__enter__()
        try:
            response = await arun_agent(
                messages,
                state,
                self._ctx,
                get_full_history_messages=get_full_history_messages,
                persist_messages_callback=persist_messages_callback,
                # Корутинные двойники, а не синхронные имена — и это несущая
                # деталь, а не стиль. _acall_blocking_hook корутинную функцию
                # ждёт НАТИВНО, а обычную уводит в asyncio.to_thread. Передать
                # сюда синхронное имя значит вернуть поток из пула, занятый на
                # всё время суммаризации, ради которого всё и переводилось.
                pre_backend_compaction_hook=summary.apre_backend_compaction_hook,
                turn_end_compaction_hook=summary.aturn_end_compact_if_needed,
                turn_closed_hook=self._arecord_turn_outcome,
                force_compact_hook=summary.aforce_compact_once,
                get_pending_messages=self._take_pending_messages,
                check_stop=self._is_stop_requested,
                session_meta=(
                    thread_session.meta if thread_session is not None else None
                ),
                mark_finalizing=(
                    lambda active: aset_tui_finalizing(thread_session, active)
                ),
                mark_finish_gate_checking=(
                    lambda active: aset_tui_finish_gate_checking(
                        thread_session, active
                    )
                ),
            )
            loop_finished = True
            self.last_terminal_outcome = getattr(
                state, "terminal_outcome", None
            )
            self.last_closing_reply = getattr(state, "closing_reply", "")
        except BaseException as exc:
            run_error = exc
            raise
        finally:
            _stop_bound.__exit__(None, None, None)
            # Выбор, так и не дошедший до вызова; запись смены пишут после
            # итога хода — так её строку показывает лента.
            choice_record = note_applied_at_run_end(state, self._ctx)
            state_ops.leave(state)
            state.turn_level = None
            try:
                # Ход, уже записанный из цикла, — no-op. Под отменой
                # счётчик задачи не трогаем: отмена уже доставлена, запись
                # доходит, а `asyncio.timeout` и TaskGroup вызывающего
                # опираются на `cancelling()`.
                await self._arecord_turn_outcome(None, error=run_error)
                await self._arecord_model_choice(choice_record)
            finally:
                teardown_cancel = await self._ateardown_run(_owner_reset)

        if teardown_cancel is not None:
            # Незавершённый цикл сюда не попадает: его исключение выходит из
            # try/finally само, и чужой итог мы не записываем.
            if loop_finished:
                await self._apersist_finished_loop_during_cancel()
            raise teardown_cancel

        # Учёт — одна точка. Кто продолжит прогон после возврата, закроет её сам.
        # Отмена уборки сюда не доходит: завершённый цикл уже записан выше.
        if account_run:
            await self.aclose_run_accounting()

        self._report_log_path(logger)
        return response

    def set_permission_mode(self, mode: str) -> None:
        """Сменить режим разрешений сессии (хоткей фронтенда, флаг запуска).

        Резолвер читает режим на каждом решении, поэтому следующий вызов тула
        пойдёт уже по новому режиму — и посреди прогона. Выбор запоминается
        ВСЕГДА, а не только при живом state: между прогонами ``self._state``
        принадлежит завершённому прогону, запись в него никуда не поедет, а
        следующий прогон соберёт shared заново из сессии.
        """
        resolved = coerce_mode(mode)
        if resolved is None:
            raise ValueError(f"unknown permission mode: {mode!r}")
        with self._lock:
            self._permission_mode_choice = resolved.value
            if self._state is not None:
                self._state.shared.permission_mode = resolved.value

    def clear_permission_mode(self) -> None:
        """Снять явный выбор режима: следующий прогон возьмёт режим из
        сессии, а нет его там — из конфига.

        Живое состояние не трогается: между прогонами оно принадлежит
        завершённому прогону, а следующий соберёт shared из сессии заново.
        До следующего прогона ``permission_mode()`` может вернуть выбор
        прошлого прогона.
        """
        with self._lock:
            self._permission_mode_choice = None

    def permission_mode(self) -> Optional[str]:
        """Режим, выбранный явно или сохранённый в сессии; None — действует конфиг."""
        with self._lock:
            if self._permission_mode_choice is not None:
                return self._permission_mode_choice
            if self._state is not None:
                return getattr(self._state.shared, "permission_mode", None)
            return None

    def add_message(self, message: dict) -> None:
        """Потокобезопасно добавляет сообщение в очередь активного скоупа.

        Executing subagent → child pending; иначе → parent pending.
        """
        # Адресат берётся из СВОЕГО контекста: сообщение этого прогона не может
        # уехать субагенту чужой сессии — до её контекста отсюда нет ссылки.
        if try_enqueue_user_message(self._ctx, message):
            return
        with self._lock:
            self._pending_messages.append(message)

    def withdraw_message(self, msg_id: str) -> bool:
        """Забрать из очереди сообщение, которое агент ещё не впустил.

        Вместе с ним уходит его технический спутник
        (`companion_message_id`). Под замком очереди сообщение либо снято
        здесь, либо уже впущено (`_take_pending_messages`) — третьего нет.
        Очередь субагента не смотрим: ввод ребёнку в очередь не ставят.
        """
        with self._lock:
            return withdraw_from_queue(
                self._pending_messages, msg_id
            ) or withdraw_from_queue(self._undelivered_messages, msg_id)

    def is_child_input_active(self) -> bool:
        """Работает ли сейчас субагент, которому уйдёт ввод пользователя.

        Фронтенду нужно спрашивать СВОЕГО исполнителя, а не модуль ядра:
        модульная функция отвечала за весь процесс и в многосессионном сервере
        говорила о чужом прогоне.
        """
        return is_child_input_active(self._ctx)

    def active_parent_call_id(self) -> str | None:
        """``call_id`` вызова субагента, который сейчас принимает ввод."""
        return active_parent_call_id(self._ctx)

    async def aflush_thread(self) -> None:
        """Записать отложенные шаги хода (persist: turn); иначе — ничего."""
        flush = getattr(self._thread_session, "aflush_deferred", None)
        if flush is not None:
            await flush()

    def get_state(self) -> State | None:
        """
        Возвращает текущий state (под замком).
        Не мутировать снаружи; изменения — только через add_message() или под замком executor'а.
        """
        with self._lock:
            return self._state

    def get_model_router(self):
        """Router, зафиксированный при сборке ЭТОГО executor'а (``RunContext.model_router``,
        см. ``src/run_context.py``) — снимок ``models``/``slots``/``assign`` неизменен всю
        его жизнь (пересборка — только следующим ``get_runner()`` после ``invalidate_runner``).

        Футеру TUI нужен именно этот Router для резолва подготовленного вызова по
        его уровню: живой ``display_router()`` отражает /profile мгновенно и подменил бы
        факт моделью уже переключившегося профиля (резолв — перед каждым вызовом по
        живому уровню, ``processing_responses.resolve_main_for_call``). ``None`` — у
        ``ctx`` нет ``model_router`` (тестовые двойники).
        """
        return getattr(self._ctx, "model_router", None)

    def _history_summary_limits(self):
        """Пороги сжатия модели текущего вызова (не снимок старта run):
        по уровню, выбранному перед подготовкой запроса, — тому же, по которому идут
        запросы. До сборки state уровня ещё нет — обычный резолв `main`."""
        return resolve_summary_limits_at_level(
            self._ctx,
            self.get_model_router(),
            getattr(self._state, "_call_level", None),
        )

    def summary_limits(self):
        """Пороги сжатия, по которым работает ЭТОТ executor: его снимок конфига,
        его Router, уровень текущего вызова. Читает индикатор контекста TUI — новый
        выбор пользователя не меняет уже подготовленный вызов."""
        return self._history_summary_limits()

    def usage_meta_snapshot(self) -> Optional[tuple[dict, Optional[dict]]]:
        """Снимок usage для TUI-футера: ``(session_meta, request_meta | None)``.

        ``session_meta`` — кумулятив треда (``state.usage``), тикает вживую.
        ``request_meta`` — «запрос»:
        - ход идёт (``last_run_usage`` ещё None) → **live-дельта** ``usage − baseline``,
          где baseline (``run_start_usage_meta``) снят на старте хода: число
          сбрасывается с новым вопросом и растёт по мере работы агента;
        - ход завершён (``last_run_usage`` выставлен) → итог этого хода;
        - прогонов ещё не было → None.
        Всё через ``to_meta()`` под замком — против гонки «dictionary changed size»
        с живым ``UsageAccount.add()`` в agent-потоке. None, если state не создан.
        """
        with self._lock:
            state = self._state
            if state is None:
                return None
            usage = getattr(state, "usage", None)
            session_meta = usage.to_meta() if usage is not None else {}
            last_run = getattr(state, "last_run_usage", None)
            if last_run is not None:
                request_meta: Optional[dict] = last_run.to_meta()
            else:
                baseline_meta = getattr(state, "run_start_usage_meta", None)
                if baseline_meta is not None and usage is not None:
                    baseline = UsageAccount.from_meta(baseline_meta)
                    request_meta = usage.delta_from(baseline).to_meta()
                else:
                    request_meta = None
            return (session_meta, request_meta)

    @staticmethod
    def _build_user_messages(
        user_bodies,
        *,
        message_ids,
        display_contents,
        skills_loaded,
        gen_id,
        attachments=None,
        commit=None,
    ):
        """Собрать user-сообщения с опциональными display_content/skill_loaded/attachments.
        display_content ставится только при отличии от content; skill_loaded — если задан;
        attachments — если у сообщения есть вложения: каждое проходит через ``commit``
        (копия в тред прогона), ``commit=None`` — вложения как есть, в кармане."""
        out = []
        for i, body in enumerate(user_bodies):
            body = str(body)
            msg = {
                "role": "user",
                "content": body,
                "id": (
                    message_ids[i]
                    if message_ids and i < len(message_ids) and message_ids[i]
                    else gen_id("m")
                ),
            }
            disp = (
                display_contents[i]
                if display_contents and i < len(display_contents)
                else None
            )
            if disp is not None and disp != body:
                msg["display_content"] = disp
            sk = (
                skills_loaded[i]
                if skills_loaded and i < len(skills_loaded)
                else None
            )
            if sk:
                msg["skill_loaded"] = sk
            _put_attachments(msg, attachments, i, commit)
            out.append(msg)
        return out

    def _commit_queued(self, messages: list[dict]) -> list[dict]:
        """Сообщения очереди с вложениями, переложенными в тред прогона: сообщение
        получает тред, когда его принимает агент, — здесь и копируем его картинки."""
        return [with_committed_attachments(m, self._ctx) for m in messages]

    async def _arecord_turn_outcome(
        self,
        reason: Optional[str] = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        """Записать итог текущего хода — не больше одного раза на ход.

        Зовут хук цикла (``_aclose_turn``: финал, принятый текст, план) и
        ``finally`` прогона (остальные выходы). Признак ``recorded`` ставится
        до записи: повтор ничего не пишет, даже если первая запись упала.
        Ход, не положивший в историю ни одного сообщения (стоп в сетапе),
        записи не получает: его на диске нет. Сбой сборки или записи —
        предупреждение в журнал прогона, ход он не роняет."""
        clock = current_turn_clock(self._ctx)
        state = self.get_state()
        session = self._thread_session
        if clock is None or clock.recorded or state is None:
            return
        clock.recorded = True
        clock.close()
        if session is None or state.turn_messages_start is None:
            return
        status, why = self._classify_turn_end(reason, error)
        try:
            outcome = TurnOutcome(
                status=status,
                reason=why,
                opened_by=opening_reply_id(state),
                started_at=clock.started_at,
                ended_at=clock.ended_at,
                duration_sec=round(clock.elapsed(), 1),
                error=_error_fact(error) if status == "error" else None,
                calls=clock.calls() or None,
            )
            await session.aappend_turn_outcome(
                outcome, discarded=is_persist_discarded(state)
            )
        except Exception:
            logger = getattr(self._ctx, "logger", None)
            if logger is not None:
                logger.warning("turn_outcome_write_failed", exc_info=True)

    def _classify_turn_end(
        self, reason: Optional[str], error: BaseException | None
    ) -> tuple[TurnStatus, Optional[str]]:
        """Исход хода: стоп сильнее всего (исключение после запроса стопа —
        его следствие), затем отмена, ошибка, причина из цикла, лимит.

        Стоп пользователя — запрошенный `stop_run` или Ctrl+C; отмена
        задачи без запроса — `cancelled`. Не различается: aio-поток при
        отмене (таймаут, отвалившийся клиент) сам зовёт `stop_run` перед
        `task.cancel()`, и такой ход пишется стопом пользователя."""
        if self._is_stop_requested() or isinstance(error, KeyboardInterrupt):
            return "stopped", USER_STOP
        if isinstance(error, asyncio.CancelledError):
            return "stopped", CANCELLED
        if error is not None:
            return "error", None
        if reason is not None:
            return "done", reason
        outcome = getattr(self.get_state(), "terminal_outcome", None)
        if outcome in _LIMIT_OUTCOMES:
            return "stopped", outcome
        return "done", None

    def stop_run(self) -> None:
        """Выставляет флаг остановки; агент завершит текущую итерацию и выйдет.

        Если state ещё не создан (run() в setup-фазе, напр. сборка в worker-
        потоке) — запоминаем в _stop_pending, run() применит при создании state."""
        with self._lock:
            if self._state is not None:
                self._state.stop_requested = True
            else:
                self._stop_pending = True
            run_logger = getattr(self._ctx, "logger", None)
        # Момент стопа — конец хода: поток, доработавший после него, часы
        # уже не двигает.
        clock = current_turn_clock(self._ctx)
        if clock is not None:
            clock.close()
        self.user_stop.request(run_logger)

    def turn_effective_start(self) -> float | None:
        """Старт текущего хода, сдвинутый на ожидания человека (монотоника
        рантайма) — для живого счёта фронтенда. Часов нет или они закрыты
        (ход кончился, часы следующего ещё не привязаны) — ``None``."""
        clock = current_turn_clock(self._ctx)
        if clock is None or clock.closed:
            return None
        return clock.effective_start()

    def turn_call_elapsed(self, call_id: str) -> float | None:
        """Время вызова текущего хода по его часам (идущего — живое,
        законченного — как в записи итога) — для таймера строки фронтенда.
        Часов нет или вызов им не известен — ``None``."""
        clock = current_turn_clock(self._ctx)
        return clock.call_elapsed(call_id) if clock is not None else None

    async def acompact_thread(
        self, thread_id: str, system: str | None = None
    ) -> bool:
        """Форс-сжатие треда (ручной /compact) — делегат в manual_compaction
        (оркестрация суммаризации, вне run-loop)."""
        from gc_core.manual_compaction import (
            acompact_thread as _acompact_thread,
        )

        return await _acompact_thread(
            ctx=self._ctx,
            _lock=self._lock,
            self_state=self._state,
            thread_id=thread_id,
            system=system,
        )
