"""
Потокобезопасный исполнитель агента.

Один мьютекс на экземпляр, держится кратко (доступ к state / pending / stop).
State создаётся заново при каждом run(). Возвращённый из get_state() state
нельзя мутировать снаружи; изменения — только через add_message() или под get_lock().
"""

import gc
import threading
from copy import copy as _shallow_copy_msg
from typing import Any, Optional

from gc_core.processing_responses import (
    _acall_blocking_hook,
    arun_agent,
    begin_turn_main_trace,
)
from gc_core.run_context import RunContext
from gc_core.config_modules.stored_outputs import get_stored_tool_outputs_max_entries
from gc_core.message_policy.chat_completions import repair_tool_call_pairing
from gc_core.message_policy.stop_persist import is_persist_discarded
from gc_core.run_summary_coordinator import (
    RunSummaryCoordinator,
    areset_stale_summarizing_flag,
    aset_tui_finalizing,
    aset_tui_finish_gate_checking,
)
from gc_core.summarize.config import parse_summary_trigger_chars, parse_summary_turn_end_trigger_chars
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.intent_log.user_turn import append_user_ask_deduped, append_user_asks_from_messages
from gc_core.message_policy.user_message_policy import extract_real_user_message_text
from gc_core.states import State, SharedStateData
from gc_core.usage.account import UsageAccount
from gc_core.usage.report import build_run_usage_report, build_run_usage_dict
from gc_core.thread_store import ThreadSession, make_message_key, resolve_threads_dir
from gc_core.trace_id import trace_id_from_config
from gc_core.tools.subagents.active_scope import try_enqueue_user_message
from gc_core.tools.subagents.selection import subagents_enabled
from gc_core.tools.subagents.sidecar import bind_thread_identity, clear_thread_identity
from gc_core.tools.connect_mcp.persistent_stdio import PersistentStdioRegistry
from gc_core.mcp_session_owner import reset_owner, set_owner
from gc_core.tools.connect_mcp.stream_client import _SESSION_MANAGER as _STREAM_SESSION_MANAGER
from gc_core.message_policy.tool_usage_notice.state import persist_tool_usage_notice_state
from gc_core.utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func
from gc_core.utils.logging_module import human_console_enabled
from gc_core.config_modules.agent import merge_special_instructions_with_model_steering
from gc_core.utils.local_render_system_prompt import render_system_prompt
from gc_core.security.permission_store import PermissionStore

ORCHESTRATOR_SPECIFIC_ROLE = (
    "You are an orchestrator agent. You may execute work yourself or delegate to subagents when that clearly pays off.\n"
    "- Handle simple, local tasks yourself (small edits, one-off questions, quick checks) — do not delegate by default.\n"
    "- Delegate only when the task is genuinely complex, parallelizable, or needs a specialized role "
    "(e.g. deep review, large exploration) and delegation tools are listed in `<tools>`.\n"
    "- For complex work, decompose into subtasks and delegate appropriately; validate outcomes yourself "
    "or via a review subagent when quality or risk warrants it.\n"
    "- At the start of a large task, use a planner subagent only when planning overhead is justified.\n"
)


def _append_trace_id_to_meta(
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
    system_id = getattr(thread_session, "system_id", None)
    thread_id = getattr(thread_session, "thread_id", None)
    if store is None or not system_id or not thread_id:
        return
    try:
        sys_norm = sanitize_system_id(system_id)
        meta = store.load_meta(sys_norm, thread_id) or {}
        existing = runtime_section(meta).get("trace_ids")
        ids = [str(x) for x in existing] if isinstance(existing, list) else []
        if trace_id in ids:
            return
        ids.append(trace_id)
        store.update_runtime_meta(sys_norm, thread_id, {"trace_ids": ids})
    except Exception as exc:
        if logger is not None:
            try:
                logger.debug("trace_id_meta_write_failed error=%s", exc, exc_info=True)
            except Exception:
                pass
        return


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
        await port.aupdate_runtime_meta(sys_norm, thread_id, {"trace_ids": ids})
    except Exception as exc:
        if logger is not None:
            try:
                logger.debug("trace_id_meta_write_failed error=%s", exc, exc_info=True)
            except Exception:
                pass
        return




class AgentExecutor:
    """
    Потокобезопасный запуск агента с возможностью добавлять сообщения во время run
    и останавливать выполнение через stop_run().
    """

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()
        self._pending_messages: list[dict] = []
        self._state: State | None = None
        # stop_run() до создания state (run() ещё в setup-фазе): запоминаем, и
        # run() применит флаг сразу при создании state, до первого persist.
        self._stop_pending: bool = False
        self.last_terminal_outcome: Optional[str] = None

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
    ) -> Any:
        """Прогон агента, не блокирующий вызывающий loop.

        При каждом вызове state создаётся заново. messages — непустой список
        текстов, каждый уходит отдельным user-сообщением.

        Форма ОДНА. Синхронного ``run`` нет: из синхронного кода агента
        ведут своим ``asyncio.run`` в рабочем потоке — так делают TUI и
        harbor-адаптер.
        """
        config_dict = self._ctx.config_dict
        logger = getattr(self._ctx, "logger", None)
        effective_verbose = (
            verbose if verbose is not None else config_dict.get("verbose", False)
        )
        skills_list = self._ctx.skills_registry.list_skills()

        thread_session: ThreadSession | None = None
        thread_shared: SharedStateData | None = None
        previous_response_id: str | None = None
        backend_api_mode = str(config_dict.get("backend_api_mode", "responses")).strip().lower()
        if thread_id is not None:
            system_id = system or "cli"
            if start_new_thread:
                # Сессии ещё нет, поэтому хранилище берём из контекста; нет и
                # там — файловое, как раньше.
                _reset_store = getattr(self._ctx, "thread_store", None)
                if _reset_store is None:
                    from gc_core.thread_store_file import FileThreadStore

                    _reset_store = FileThreadStore(resolve_threads_dir(config_dict))
                from gc_core.thread_store_async import athread_store

                await athread_store(_reset_store).areset(system_id, thread_id)
            thread_session = await ThreadSession.afrom_config(
                config_dict=config_dict,
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
        else:
            system_id = system or "cli"
            clear_thread_identity(self._ctx)
        is_cli_mode = system_id == "cli"

        # Снять протухший флаг компакции от прерванного прошлого run'а: на старте
        # компакция не идёт, иначе TUI вечно показывает «сжимаю контекст».
        await areset_stale_summarizing_flag(thread_session)
        # Снять протухший флаг финализации от прерванного прошлого run'а.
        await aset_tui_finalizing(thread_session, False)
        # Снять протухший флаг «судья работает» от прерванного прошлого run'а.
        await aset_tui_finish_gate_checking(thread_session, False)

        # Снимок MCP «сервер→тулы» для TUI-экрана /mcp (необязателен — не валим run).
        if thread_id is not None and config_dict.get("mcp-servers"):
            try:
                from gc_core.tools.mcp_snapshot import build_mcp_tools_snapshot
                snap = build_mcp_tools_snapshot(config_dict, self._ctx.tools_data, self._ctx.mcp_status)
                await thread_session.port.aupdate_runtime_meta(
                    system_id, thread_id, {"mcp_tools": snap},
                )
            except Exception:
                pass

        summary_trigger_chars = parse_summary_trigger_chars(config_dict)
        turn_end_trigger_chars = parse_summary_turn_end_trigger_chars(config_dict)

        chat_completions_system_prompt: str | None = None
        rendered_system_prompt: str | None = None
        special_instructions: str | None = None
        if backend_api_mode == "chat_completions":
            prompt_path = config_dict.get("chat_completions_system_prompt_path")
            if isinstance(prompt_path, str) and prompt_path.strip():
                try:
                    with open(prompt_path, "r", encoding="utf-8") as fd:
                        chat_completions_system_prompt = fd.read().strip()
                # Путь берётся из конфига — файл человека, кодировка его.
                # `UnicodeDecodeError` подкласс `ValueError`, а не `OSError`: он
                # уходил мимо этой ветки, то есть run падал БЕЗ объяснения, какой
                # файл виноват.
                except (OSError, UnicodeDecodeError) as e:
                    if effective_verbose:
                        print(
                            f"Не удалось прочитать chat_completions system prompt: {prompt_path}"
                        )
                    if logger is not None:
                        logger.warning(
                            "system_prompt_read_failed",
                            extra={"event": "system_prompt_read_failed", "path": prompt_path},
                        )
                    raise e

            # Клиент-специфичные инструкции (напр. TUI) — отсутствие/нечитаемость
            # файла деградирует тихо (None), не роняя run.
            si_path = config_dict.get("chat_completions_special_instructions_path")
            if isinstance(si_path, str) and si_path.strip():
                try:
                    with open(si_path.strip(), "r", encoding="utf-8") as fd:
                        special_instructions = fd.read().strip() or None
                except (OSError, UnicodeDecodeError):
                    special_instructions = None
            special_instructions = merge_special_instructions_with_model_steering(
                special_instructions,
                config_dict=config_dict,
            )

        state_data = {
            "limit_project_metadata": config_dict.get("limit_project_metadata", False),
            "parent_state_id": None,
            "verbose": effective_verbose,
            "model": config_dict["model"],
            "tools_spec": self._ctx.tools_spec,
            "tools_descr": self._ctx.tools_description,
            "max_steps": config_dict["max_steps"],
            "skills_list": skills_list,
            "system_prompt_id": config_dict["system_prompt_id"],
            "chat_completions_system_prompt": chat_completions_system_prompt,
            "use_server_subagents": config_dict.get("use_server_subagents", False),
            "agent_md_path": config_dict.get("agent_md_path"),
            "is_tui": bool(getattr(self._ctx, "is_tui", False)),
            "agentsmd_override": bool(config_dict.get("agentsmd_override", False)),
            "thread_id": thread_id,
            "shared": thread_shared,
            "workspace_config": config_dict,
            "logger": logger,
            "workspace": getattr(self._ctx, "workspace", None),
        }
        if subagents_enabled(config_dict.get("use_local_subagents", False)):
            state_data["specific_role"] = ORCHESTRATOR_SPECIFIC_ROLE
        state = State(**state_data)
        # Старт рана — первая из двух точек начала хода (спека §6.4): фиксируем
        # модель этого хода через Router, дальше внутри хода не перечитываем.
        # Открытый активный алиас (trip прошлого хода / персист breaker при attach)
        # → честная ошибка класса 2 ДО транспорта (спека §8), не сырой
        # AliasCircuitOpenError на старте рана.
        begin_turn_main_trace(state, self._ctx)
        state.tool_call_outputs_store.set_max_entries(
            get_stored_tool_outputs_max_entries(config_dict)
        )
        state.backend_api_mode = backend_api_mode
        state.on_messages_evicted = getattr(self._ctx, "on_messages_evicted", None)
        state.dialog_steps_offset = 0
        if thread_session is not None:
            meta = getattr(thread_session, "meta", None) or {}
            state.init_user_intent_log(meta.get("user_intent_log"))
            state.init_current_user_intent(meta.get("current_user_intent"))
            state.init_user_turn_seq(meta.get("user_turn_seq"))
            state.infer_user_turn_seq_if_missing()
            state.init_tool_usage_notice_from_meta(meta)
            state.dialog_steps_offset = int(meta.get("dialog_steps_offset", 0) or 0)
            state.usage = UsageAccount.from_meta(meta.get("token_usage"))
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
        with self._lock:
            self._state = state
            self._state.discard_writes_after_stop = bool(discard_writes_after_stop)
            # Применить stop, пришедший пока state ещё не существовал (setup-фаза),
            # чтобы первый же check_stop вышел ДО persist user-сообщения.
            if self._stop_pending:
                self._state.stop_requested = True
                self._stop_pending = False
            self._pending_messages = []

        # Permission memory: store оборачивает state.shared.confirm_rules ПО ССЫЛКЕ —
        # session-правила пишутся прямо в shared и сохраняются обычной персистентностью.
        setattr(
            self._ctx, "permission_store",
            PermissionStore(session_rules=state.shared.confirm_rules),
        )

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

        summary = RunSummaryCoordinator(
            ctx=self._ctx,
            state=state,
            thread_session=thread_session,
            summary_trigger_chars=summary_trigger_chars,
            turn_end_trigger_chars=turn_end_trigger_chars,
            is_cli_mode=is_cli_mode,
            pre_backend_trigger_reason="pre_backend",
            logger=logger,
        )
        setattr(self._ctx, "on_summary_applied", summary.on_summary_applied)
        setattr(self._ctx, "is_cli_mode", is_cli_mode)

        # Хук передаётся КОРУТИННЫМ именем: `_acall_blocking_hook` ждёт
        # такой нативно и в поток не уводит. Синхронное имя здесь было бы не
        # просто медленнее — fail-open внутри компакции проглотил бы отказ, и
        # она выключилась бы целиком, ничего не сказав. Стережёт
        # `test_loop_hooks_are_native`.
        await _acall_blocking_hook(
            summary.apre_run_compact_if_needed, trigger_reason="pre_run"
        )

        if not messages:
            raise ValueError(
                "Список пользовательских сообщений не должен быть пустым."
            )
        user_bodies = [str(x) for x in messages]
        # id можно задать снаружи (TUI пробрасывает свой, чтобы потом точечно
        # откатить осиротевшую реплику при быстрой отмене); иначе генерим.
        new_user_messages = self._build_user_messages(
            user_bodies, message_ids=message_ids,
            display_contents=display_contents, skills_loaded=skills_loaded,
            gen_id=generate_message_id_func,
        )
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

        messages = list(new_user_messages)

        persist_messages_callback = None
        get_full_history_messages = None
        if thread_session is not None:
            state.previous_response_id = previous_response_id
            thread_session.set_runtime_messages(state.messages)

            # chat/completions: история уже в state.messages (extend выше).
            # В run_agent передаём только новый user-батч — backend собирает payload
            # из state + batch без дублей (см. create_call_data_completions).
            if backend_api_mode == "chat_completions":
                base_messages = list(thread_session.history_messages)
                state.skip_initial_user_ask_step_when_history = len(base_messages) > 0
                messages = list(new_user_messages)
            else:
                base_messages = (
                    list(thread_session.history_messages)
                    if previous_response_id is None
                    else []
                )
                state.skip_initial_user_ask_step_when_history = len(base_messages) > 0
                messages = [*base_messages, *new_user_messages]

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
                    m for m in step_messages
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
                if isinstance(last_backend_response_id, str) and last_backend_response_id:
                    thread_session.meta["last_backend_response_id"] = (
                        last_backend_response_id
                    )
                await thread_session.apersist_batch(
                    step_messages, previous_response_id=state.previous_response_id
                )

            persist_messages_callback = _persist_thread_batch
            get_full_history_messages = thread_session.get_full_history_messages
        elif backend_api_mode == "chat_completions" and summary_trigger_chars > 0:
            def _persist_runtime_batch(step_messages: list[dict]) -> None:
                if is_persist_discarded(state):
                    return
                state.current_user_intent = append_user_asks_from_messages(
                    state.current_user_intent,
                    step_messages,
                    user_turn_seq=state.user_turn_seq,
                )

            persist_messages_callback = _persist_runtime_batch

        def get_pending_messages() -> list[dict]:
            with self._lock:
                out = self._pending_messages[:]
                self._pending_messages.clear()
                return out

        def check_stop() -> bool:
            with self._lock:
                return self._state.stop_requested if self._state is not None else False

        # Снимок usage до прогона: для resume треда state.usage гидрирован кумулятивом,
        # отчёт же показывает токены именно этого прогона (дельту). Тот же baseline
        # кладём на state — по нему TUI-футер считает live-«запрос» текущего хода.
        run_start_usage = UsageAccount.from_meta(state.usage.to_meta())
        state.run_start_usage_meta = run_start_usage.to_meta()
        # Входим во владение MCP-сессиями этого контекста. Всё, что поднимут
        # тулы внутри прогона, ляжет под этот токен — и на выходе мы закроем
        # ровно своё, не тронув сессии параллельных агентов на том же loop.
        _owner_reset = set_owner(getattr(self._ctx, "mcp_session_owner", "") or None)
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
                force_compact_hook=summary.aforce_compact_once,
                get_pending_messages=get_pending_messages,
                check_stop=check_stop,
                session_meta=(
                    thread_session.meta
                    if thread_session is not None
                    else None
                ),
                mark_finalizing=(
                    lambda active: aset_tui_finalizing(thread_session, active)
                ),
                mark_finish_gate_checking=(
                    lambda active: aset_tui_finish_gate_checking(thread_session, active)
                ),
            )
            self.last_terminal_outcome = getattr(state, "terminal_outcome", None)
            if thread_session is not None and not (
                state.discard_writes_after_stop and state.stop_requested
            ):
                thread_session.meta = ThreadMetaMapper.merge_state_into_meta(
                    thread_session.meta, state
                )
                thread_session.meta["backend_api_mode"] = state.backend_api_mode
                persist_tool_usage_notice_state(state, thread_session.meta)
                await thread_session.apersist_batch(
                    [], previous_response_id=state.previous_response_id
                )
                _run_trace_id = trace_id_from_config(config_dict)
                if _run_trace_id:
                    # После persist_batch: он пересобирает мету с диска, поэтому
                    # запись должна быть последней, иначе её затрёт.
                    await _aappend_trace_id_to_meta(thread_session, _run_trace_id, logger)
        finally:
            # Закрываем долгоживущие stdio MCP-сессии (напр. @playwright/mcp + Chromium),
            # чтобы они не переживали конкретный запуск агента.
            #
            # Ждём напрямую, без потока. Закрытие небыстрое (terminate →
            # таймаут до 5с → killpg), но блокирующим в нём было только
            # ожидание смерти процесса, и оно переведено на опрос: loop
            # свободен, поток из пула не занят. Владелец берётся из contextvar
            # здесь же, на своём стеке, — закроется только наше, чужой браузер
            # продолжит работать.
            await PersistentStdioRegistry.instance().aclose_all()
            # И persistent-сессии stream_* транспортов (stream_stdio/stream_sse/stream_http).
            try:
                # НЕ через to_thread: сессии ключуются по id(loop) и живут на
                # ЭТОМ loop — из чужого потока их не видно и закрыть нечем.
                # Закрытие асинхронное и быстрое (сеть + пайпы).
                await _STREAM_SESSION_MANAGER.aclose_all()
            except Exception:
                pass
            # C2: после закрытия MCP/stdio — подбираем циклический мусор
            # (pipes, ClientSession, loop buffers). State здесь не трогаем:
            # ниже usage-отчёт по state.
            gc.collect()
            # Владение снимаем последним: всё выше в этом finally его требует.
            reset_owner(_owner_reset)

        # Отчёт по токенам/стоимости за этот прогон (дельта от снимка до старта).
        try:
            run_usage = state.usage.delta_from(run_start_usage)
            # Доступно in-memory: get_state().usage (кумулятив) и .last_run_usage (этот прогон).
            state.last_run_usage = run_usage
            usage_text = build_run_usage_report(
                run_usage,
                config_dict.get("pricing"),
                config_dict.get("model"),
                config_dict.get("completions_url"),
            )
            if usage_text:
                if logger is not None:
                    logger.info("\n<run_usage>\n%s\n</run_usage>\n", usage_text)
                if human_console_enabled(config_dict):
                    print(f"\n{usage_text}\n")
            if logger is not None:
                usage_dict = build_run_usage_dict(
                    run_usage,
                    config_dict.get("pricing"),
                    config_dict.get("model"),
                    config_dict.get("completions_url"),
                )
                if usage_dict:
                    logger.info(
                        "run_usage",
                        extra={"event": "run_usage", "usage": usage_dict},
                    )
        except Exception as usage_exc:  # отчёт не должен ронять прогон
            if logger is not None:
                logger.warning("run usage report failed: %s", usage_exc, exc_info=True)

        if human_console_enabled(config_dict):
            print(f"\nПуть к файлу с логами: {self._ctx.logging_path}\n")
        if logger is not None:
            logger.info(
                "log_path",
                extra={"event": "log_path", "path": getattr(self._ctx, "logging_path", None)},
            )
        return response


    def add_message(self, message: dict) -> None:
        """Потокобезопасно добавляет сообщение в очередь активного скоупа.

        Executing subagent → child pending; иначе → parent pending.
        """
        if try_enqueue_user_message(message):
            return
        with self._lock:
            self._pending_messages.append(message)

    def get_state(self) -> State | None:
        """
        Возвращает текущий state (под замком).
        Не мутировать снаружи; изменения — только через add_message() или под get_lock().
        """
        with self._lock:
            return self._state

    def get_lock(self) -> threading.Lock:
        """
        Возвращает мьютекс executor'а.
        Изменения state — только под этим замком.
        """
        return self._lock

    def get_model_router(self):
        """Router, зафиксированный при сборке ЭТОГО executor'а (``RunContext.model_router``,
        см. ``src/run_context.py``) — снимок ``models``/``slots``/``assign`` неизменен всю
        его жизнь (пересборка — только следующим ``get_runner()`` после ``invalidate_runner``).

        Футеру TUI (Task 15) нужен именно этот Router для резолва уровня, зафиксированного
        на ход (``state.turn_level``): живой ``display_router()`` TUI отражает /profiles
        мгновенно и подменил бы «факт» на модель уже переключившегося профиля раньше, чем
        ход её реально увидит. ``None`` — у ``ctx`` нет ``model_router`` (тестовые двойники).
        """
        return getattr(self._ctx, "model_router", None)

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
    def _build_user_messages(user_bodies, *, message_ids, display_contents,
                             skills_loaded, gen_id):
        """Собрать user-сообщения с опциональными display_content/skill_loaded.
        display_content ставится только при отличии от content; skill_loaded — если задан."""
        out = []
        for i, body in enumerate(user_bodies):
            body = str(body)
            msg = {
                "role": "user",
                "content": body,
                "id": (message_ids[i] if message_ids and i < len(message_ids)
                       and message_ids[i] else gen_id("m")),
            }
            disp = display_contents[i] if display_contents and i < len(display_contents) else None
            if disp is not None and disp != body:
                msg["display_content"] = disp
            sk = skills_loaded[i] if skills_loaded and i < len(skills_loaded) else None
            if sk:
                msg["skill_loaded"] = sk
            out.append(msg)
        return out

    def stop_run(self) -> None:
        """Выставляет флаг остановки; агент завершит текущую итерацию и выйдет.

        Если state ещё не создан (run() в setup-фазе, напр. сборка в worker-
        потоке) — запоминаем в _stop_pending, run() применит при создании state."""
        with self._lock:
            if self._state is not None:
                self._state.stop_requested = True
            else:
                self._stop_pending = True

    async def acompact_thread(self, thread_id: str, system: str | None = None) -> bool:
        """Форс-сжатие треда (ручной /compact) — делегат в manual_compaction
        (оркестрация суммаризации, вне run-loop)."""
        from gc_core.manual_compaction import acompact_thread as _acompact_thread
        return await _acompact_thread(
            ctx=self._ctx,
            _lock=self._lock,
            self_state=self._state,
            thread_id=thread_id,
            system=system
        )

