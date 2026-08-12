import asyncio
import json
import logging
import os
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bundled_assets import bundled_root
from trace_id import trace_id_from_config
from config_modules.stored_outputs import get_stored_tool_outputs_max_entries
from config_modules.chat_completions import chat_completions_llm_from_config_dict
from config_modules.intent_scope import intent_scope_from_config_dict
from execution_workspace import ExecutionWorkspace
from run_summary_coordinator import RunSummaryCoordinator
from summarize.config import parse_summary_trigger_chars
from constants.tool_names import SESSION_MEMORY_TOOL_NAME
from message_policy.tool_availability import available_tool_names, tool_is_available
from message_policy.tool_usage_notice.policy import (
    get_thread_memory_registers_from_shared,
    subagent_thread_memory_bootstrap_text,
    wrap_thread_memory_technical_user_message,
)
from message_policy.tool_usage_notice.state import persist_tool_usage_notice_state
from intent_log.user_turn import append_user_asks_from_messages, init_subagent_open_turn
from run_context import RunContext
from tools.skills.registry import get_skills_registry
from states import State
from tools import (
    SUBAGENT_TOOLS_ALLOW_KEY,
    SUBAGENT_TOOLS_STOP_KEY,
    build_tools_data,
)
from tools.subagents.registry import get_subagent_class, resolve_subagent_definitions
from usage.account import CATEGORY_SUBAGENT as USAGE_CATEGORY_SUBAGENT
from usage.account import UsageAccount
from usage.context import usage_scope
from config_modules.agent import merge_special_instructions_with_model_steering
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func
from utils.local_render_system_prompt import render_system_prompt
from utils.logging_module import create_run_logger, generate_run_id
from message_policy.user_message_policy import extract_real_user_message_text
from message_policy.user_stop import cancelled_by_user_output
from tools.subagents.active_scope import activate_child_input
from tools.subagents.progress import emit_progress
from tools.subagents.sidecar import (
    bind_thread_identity,
    clear_thread_identity,
    persist_subagent_trace,
)

_log = logging.getLogger(__name__)


def _synthesis_from_summary_event(event: Dict[str, Any]) -> str:
    """Текст суммаризации для sidecar child_summary."""
    trace = event.get("summary_trace") if isinstance(event, dict) else None
    final = trace.get("final_output") if isinstance(trace, dict) else None
    if isinstance(final, dict):
        for key in ("agent_summary_text", "raw_summary"):
            text = final.get(key)
            if isinstance(text, str) and text.strip():
                return text
    payload = event.get("summary_payload") if isinstance(event, dict) else None
    if isinstance(payload, dict):
        content = payload.get("summary_message_content")
        if isinstance(content, str):
            return content
    return ""


class SubagentRunner:
    """Создаёт дочерний State и запускает run_agent для субагента."""

    def __init__(self, ctx: Any, orchestrator_state: State, config_dict: Dict[str, Any]):
        self._ctx = ctx
        self._orchestrator_state = orchestrator_state
        self._config = config_dict
        self._parent_call_id: str = ""
        # Имя тула текущего вызова: нужно сборке child_config, а до неё
        # доезжают не все методы (у _build_child_state его в сигнатуре нет).
        self._active_tool_name: str = ""
        # Уже влитый в parent снимок child.usage — для incremental delta (без double-count).
        self._usage_pushed = UsageAccount()
        # То же для счётчика вызовов тулов ребёнка: сколько его вызовов уже
        # влито в parent.tool_calls. Симметрия с usage — бюджет max_tool_calls
        # считается по всему дереву, а не только по вызовам самого родителя.
        self._tool_calls_pushed = 0

    def run(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        run_agent_func: Callable,
        *,
        parent_call_id: Optional[str] = None,
    ) -> str:
        self._parent_call_id = parent_call_id or ""
        self._active_tool_name = tool_name
        subagent_cls = get_subagent_class(tool_name, self._config)
        if subagent_cls is None:
            return self._error_response(
                tool_name=tool_name,
                code="subagent_unknown_tool",
                message=f"Unknown subagent: {tool_name}",
                retryable=False,
            )
        if not isinstance(arguments, dict):
            return self._error_response(
                tool_name=tool_name,
                code="subagent_invalid_arguments",
                message="Subagent arguments must be an object.",
                retryable=False,
            )
        return self._run_internal(tool_name, subagent_cls, arguments, run_agent_func)

    def _run_internal(
        self,
        tool_name: str,
        subagent_cls: Any,
        arguments: Dict[str, Any],
        run_agent_func: Callable,
    ) -> str:
        extra = {k: v for k, v in arguments.items() if k != "task"}
        task = arguments.get("task", "")
        model = self._orchestrator_state.default_model

        try:
            # create_instance некоторых субагентов (special_agent) делает LLM-вызов
            # генерации роли — относим его токены к категории subagent родителя.
            with usage_scope(self._orchestrator_state.usage, USAGE_CATEGORY_SUBAGENT):
                instance = asyncio.run(
                    subagent_cls.create_instance(
                        task=task,
                        model=model,
                        config_dict=self._config,
                        runtime_notices_enabled=bool(getattr(self._ctx, "is_tui", False)),
                        **extra,
                    )
                )
        except Exception as exc:
            return self._error_response(
                tool_name=tool_name,
                code="subagent_create_failed",
                message=f"Subagent {tool_name} create_instance failed: {exc}",
                retryable=True,
            )
        agent_role, initial_message = instance.get_initial_data()

        try:
            child_state = self._build_child_state(agent_role)
        except Exception as exc:
            return self._error_response(
                tool_name=tool_name,
                code="subagent_state_build_failed",
                message=f"Subagent {tool_name} child state build failed: {exc}",
                retryable=False,
            )
        user_message = {
            "role": "user",
            "content": initial_message,
            "id": generate_message_id_func("m_user_sub"),
        }
        child_meta: Dict[str, Any] = {}
        messages = self._build_initial_messages(child_state, agent_role, user_message)
        init_subagent_open_turn(child_state, initial_message)
        shared_mf = get_thread_memory_registers_from_shared(child_state.shared)
        # Bootstrap памяти директивен («update persistent registers with the thread
        # memory tool»), поэтому шлём его только если тул у ребёнка есть. Конфиг
        # здесь родительский — набор ребёнка знает только его State, собранный
        # выше уже с учётом tools/stop_tools из файла сабагента.
        bootstrap_text = (
            subagent_thread_memory_bootstrap_text(self._config, shared_mf)
            if tool_is_available(
                available_tool_names(child_state), SESSION_MEMORY_TOOL_NAME
            )
            else None
        )
        if bootstrap_text:
            messages.append(
                wrap_thread_memory_technical_user_message(
                    bootstrap_text,
                    message_id_suffix="mem_wiki_boot",
                )
            )
            child_state.messages = messages.copy()

        try:
            child_ctx = self._build_child_ctx(tool_name=tool_name)
        except Exception as exc:
            return self._error_response(
                tool_name=tool_name,
                code="subagent_ctx_build_failed",
                message=f"Subagent {tool_name} child context build failed: {exc}",
                retryable=True,
                steps=child_state.steps,
            )
        self._log_lifecycle(
            event="subagent_start",
            tool_name=tool_name,
            child_state=child_state,
            child_ctx=child_ctx,
            extra={"steps": child_state.steps},
        )
        summary_trigger_chars = parse_summary_trigger_chars(self._config)
        should_enable_summary = (
            child_state.backend_api_mode == "chat_completions"
            and summary_trigger_chars > 0
        )

        persist_messages_callback = partial(
            self._persist_runtime_step, child_state, child_meta, child_ctx
        )
        pre_backend_compaction_hook = None
        if should_enable_summary:
            summary = RunSummaryCoordinator(
                ctx=child_ctx,
                state=child_state,
                thread_session=None,
                summary_trigger_chars=summary_trigger_chars,
                is_cli_mode=False,
                pre_backend_trigger_reason="subagent_pre_backend",
                logger=_log,
            )
            # Sidecar marker when child compaction applies (append-only; no rewrite).
            _orig_on_summary = summary.on_summary_applied

            def _on_summary_applied_with_sidecar(event: Dict[str, Any]) -> None:
                _orig_on_summary(event)
                synthesis = _synthesis_from_summary_event(event)
                persist_subagent_trace(
                    child_ctx,
                    kind="child_summary",
                    payload={"synthesis": synthesis},
                )

            summary.on_summary_applied = _on_summary_applied_with_sidecar
            pre_backend_compaction_hook = summary.pre_backend_compaction_hook

        self._usage_pushed = UsageAccount()
        self._tool_calls_pushed = 0
        try:
            with activate_child_input(parent_call_id=self._parent_call_id) as scope:

                def _on_child_user_enqueue(message: Dict[str, Any]) -> None:
                    prose = extract_real_user_message_text(message)
                    if not prose:
                        return
                    emit_progress(
                        child_ctx,
                        child_state,
                        "user_message",
                        prose=prose,
                    )
                    payload: Dict[str, Any] = {
                        "role": "user",
                        "content": prose,
                    }
                    msg_id = message.get("id")
                    if isinstance(msg_id, str) and msg_id:
                        payload["id"] = msg_id
                    persist_subagent_trace(
                        child_ctx, kind="user", payload=payload
                    )

                scope.set_on_enqueue(_on_child_user_enqueue)
                result = run_agent_func(
                    messages,
                    child_state,
                    child_ctx,
                    check_stop=self._check_stop_requested,
                    persist_messages_callback=persist_messages_callback,
                    pre_backend_compaction_hook=pre_backend_compaction_hook,
                    session_meta=child_meta,
                    get_pending_messages=scope.drain,
                )
        except Exception as exc:
            self._log_lifecycle(
                event="subagent_error",
                tool_name=tool_name,
                child_state=child_state,
                child_ctx=child_ctx,
                extra={
                    "steps": child_state.steps,
                    "error_code": "subagent_run_failed",
                },
            )
            self._persist_finished(
                child_ctx,
                child_state,
                ok=False,
                finish_reason="error",
            )
            return self._error_response(
                tool_name=tool_name,
                code="subagent_run_failed",
                message=f"Subagent {tool_name} run failed: {exc}",
                retryable=True,
                steps=child_state.steps,
                child_run_id=str(getattr(child_ctx, "run_id", "") or ""),
            )
        finally:
            # Только residual delta с последнего live-tick — не полный merge
            # (иначе double-count уже запушенных токенов).
            self._push_usage_delta(child_state, child_ctx)
            self._push_tool_calls_delta(child_state)

        if result is None:
            self._log_lifecycle(
                event="subagent_end",
                tool_name=tool_name,
                child_state=child_state,
                child_ctx=child_ctx,
                extra={"steps": child_state.steps, "finish_reason": "stopped_by_user"},
            )
            emit_progress(
                child_ctx,
                child_state,
                "cancelled",
                ok=False,
                error_brief="stopped by user",
            )
            self._persist_finished(
                child_ctx,
                child_state,
                ok=False,
                finish_reason="stopped_by_user",
            )
            return self._cancelled_by_user_response(
                tool_name=tool_name,
                steps=child_state.steps,
                child_run_id=str(getattr(child_ctx, "run_id", "") or ""),
            )

        _limit_outcome = getattr(child_state, "terminal_outcome", None)
        if _limit_outcome in ("step_limit", "token_limit", "tool_call_limit"):
            self._log_lifecycle(
                event="subagent_end",
                tool_name=tool_name,
                child_state=child_state,
                child_ctx=child_ctx,
                extra={"steps": child_state.steps, "finish_reason": _limit_outcome},
            )
            emit_progress(
                child_ctx,
                child_state,
                "error",
                ok=False,
                error_brief=_limit_outcome.replace("_", " "),
            )
            self._persist_finished(
                child_ctx,
                child_state,
                ok=False,
                finish_reason=_limit_outcome,
            )
            # Pass-through: child run_agent already built the limit envelope
            # (ok=false, status, message, raw_trace_summary). Do NOT rebuild
            # our own JSON or call LLM here — spec §6.2.
            return result

        result_payload: Any = result or "Subagent finished without explicit result."
        self._log_lifecycle(
            event="subagent_end",
            tool_name=tool_name,
            child_state=child_state,
            child_ctx=child_ctx,
            extra={"steps": child_state.steps, "finish_reason": "completed"},
        )
        self._persist_finished(
            child_ctx,
            child_state,
            ok=True,
            finish_reason="completed",
        )
        return self._success_response(
            tool_name=tool_name,
            result=result_payload,
            steps=child_state.steps,
            finish_reason="completed",
            usage=child_state.usage.to_meta(),
            child_run_id=str(getattr(child_ctx, "run_id", "") or ""),
        )

    def _check_stop_requested(self) -> bool:
        return self._orchestrator_state.stop_requested

    def _persist_finished(
        self,
        child_ctx: Any,
        child_state: State,
        *,
        ok: bool,
        finish_reason: str,
    ) -> None:
        persist_subagent_trace(
            child_ctx,
            kind="finished",
            payload={
                "ok": ok,
                "finish_reason": finish_reason,
                "steps": int(getattr(child_state, "steps", 0) or 0),
            },
        )

    def _push_usage_delta(self, child_state: State, child_ctx: Optional[Any] = None) -> None:
        """Влить в parent только прирост child.usage с последнего tick (категория subagent)."""
        child_usage = getattr(child_state, "usage", None)
        if not isinstance(child_usage, UsageAccount):
            return
        delta = child_usage.delta_from(self._usage_pushed)
        if delta.is_empty():
            return
        self._orchestrator_state.usage.merge(
            delta, as_category=USAGE_CATEGORY_SUBAGENT
        )
        self._usage_pushed = UsageAccount.from_meta(child_usage.to_meta())
        if child_ctx is not None:
            emit_progress(
                child_ctx,
                child_state,
                "usage",
                usage_delta=delta.to_meta(),
            )

    def _push_tool_calls_delta(self, child_state: State) -> None:
        """Влить в parent прирост child.tool_calls с последнего tick.

        Бюджет ``agent.max_tool_calls`` — по всему дереву: вызовы ребёнка тратят
        бюджет родителя так же, как это уже сделано для токенов
        (``_push_usage_delta``). Инкрементально, по тому же принципу снимка, —
        метод зовётся и на live-tick'ах, и в finally, а double-count бюджет
        обнулил бы досрочно.

        Сам вызов ``subagent_*`` уже посчитан родителем как 1 в
        ``processing_responses`` — итого родитель тратит 1 + вызовы ребёнка.
        """
        child_calls = getattr(child_state, "tool_calls", None)
        if not isinstance(child_calls, int):
            return
        delta = child_calls - self._tool_calls_pushed
        if delta <= 0:
            return
        parent = self._orchestrator_state
        if isinstance(getattr(parent, "tool_calls", None), int):
            parent.tool_calls += delta
        self._tool_calls_pushed = child_calls

    def _persist_runtime_step(
        self,
        child_state: State,
        child_meta: Dict[str, Any],
        child_ctx: Any,
        step_messages: List[Dict[str, Any]],
    ) -> None:
        child_state.current_user_intent = append_user_asks_from_messages(
            child_state.current_user_intent,
            step_messages,
            user_turn_seq=child_state.user_turn_seq,
        )
        persist_tool_usage_notice_state(child_state, child_meta)
        # Live spend → касса родителя (футер TUI читает parent state.usage).
        self._push_usage_delta(child_state, child_ctx)
        self._push_tool_calls_delta(child_state)

    def _build_child_config(self) -> Dict[str, Any]:
        """Единственная сборка конфига дочернего рана.

        Метод общий для _build_child_state и _build_child_ctx намеренно: раньше
        словарь собирался в двух местах независимо, и они обязаны были оставаться
        синхронными — ровно на этом уже разъезжались ctx.trace_id и config_dict.

        ``stop_tools`` файла ДОПОЛНЯЕТ глобальный ``agent.stop_tools``, а не
        заменяет: глобальный уже вычтен внутри get_builtin_tools_data, а этот
        список применяется поверх, к полному набору (включая mcp_*). Снять
        глобальный запрет из файла сабагента нельзя.
        """
        child_config = {**self._config, "use_local_subagents": False}
        definition = resolve_subagent_definitions(self._config).get(
            self._active_tool_name
        )
        if definition is not None:
            child_config[SUBAGENT_TOOLS_ALLOW_KEY] = (
                list(definition.tools) if definition.tools is not None else None
            )
            child_config[SUBAGENT_TOOLS_STOP_KEY] = list(definition.stop_tools)
        return child_config

    def _build_child_state(self, agent_role: str) -> State:
        """Создать дочерний State, наследуя общие данные от оркестратора."""
        child_config = self._build_child_config()
        parent_backend = getattr(self._ctx, "os_backend", None)
        child_tools_data, child_tools_spec, child_tools_descr, _mcp_status = build_tools_data(
            child_config,
            os_backend=parent_backend,
            logger=getattr(self._ctx, "logger", None),
        )
        child_system_prompt = self._resolve_subagent_system_prompt()

        parent = self._orchestrator_state
        child_state = State(
            parent_state_id=parent.state_id,
            verbose=parent.verbose,
            model=parent.default_model,
            tools_spec=child_tools_spec,
            tools_descr=child_tools_descr,
            skills_list=parent.skills_list,
            max_steps=self._config.get("subagent_max_steps", 50),
            system_prompt_id=parent.system_prompt_id,
            use_server_subagents=False,
            chat_completions_system_prompt=child_system_prompt,
            specific_role=agent_role,
            agent_md_path=parent.agent_md_path,
            shared=parent.shared,
            workspace_config=child_config,
            logger=parent.logger,
            workspace=getattr(parent, "workspace", None),
        )
        child_state.tool_call_outputs_store.set_max_entries(
            get_stored_tool_outputs_max_entries(child_config)
        )
        child_state.backend_api_mode = "chat_completions"
        child_state.discard_writes_after_stop = parent.discard_writes_after_stop
        return child_state

    def _is_custom_subagent(self) -> bool:
        """Задан ли текущий сабагент файлом, а не классом."""
        return self._active_tool_name in resolve_subagent_definitions(self._config)

    def _resolve_subagent_system_prompt(self) -> Optional[str]:
        """Вернуть системный промпт для субагента с fallback на промпт родителя.

        У кастомных (файловых) сабагентов промпт свой: он предупреждает модель,
        что роль написана оператором в локальном файле, прав не выдаёт и может
        расходиться с фактическим набором инструментов.
        """
        if self._is_custom_subagent():
            config_key = "chat_completions_custom_subagent_system_prompt_path"
            default_name = "chat_completions_custom_subagent_system_prompt.md"
        else:
            config_key = "chat_completions_subagent_system_prompt_path"
            default_name = "chat_completions_subagent_system_prompt.md"

        prompt_path_raw = self._config.get(config_key)
        candidate_paths: List[str] = []
        if isinstance(prompt_path_raw, str) and prompt_path_raw.strip():
            candidate_paths.append(os.path.abspath(prompt_path_raw.strip()))
        # bundled_root() — единый резолвер ассетов (editable → repo, wheel → site-packages).
        candidate_paths.append(str(bundled_root() / "prompts" / default_name))

        for prompt_path in candidate_paths:
            if not prompt_path:
                continue
            try:
                if not os.path.exists(prompt_path):
                    continue
                with open(prompt_path, "r", encoding="utf-8") as fd:
                    loaded = fd.read().strip()
                if loaded:
                    return loaded
            except OSError:
                _log.warning(
                    "Failed to read subagent system prompt from %s",
                    prompt_path,
                    exc_info=True,
                )

        return self._orchestrator_state.chat_completions_system_prompt

    def _build_initial_messages(
        self,
        child_state: State,
        agent_role: str,
        user_message: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Сформировать начальный transcript (system + user) в child_state.messages."""
        messages: List[Dict[str, Any]] = []

        system_prompt = child_state.chat_completions_system_prompt
        if system_prompt:
            special_instructions = merge_special_instructions_with_model_steering(
                None,
                config_dict=self._config,
            )
            rendered = render_system_prompt(
                system_prompt=system_prompt,
                tools_spec=child_state.tools,
                project_info=child_state.initial_project_data,
                specific_role=agent_role,
                special_instructions=special_instructions,
            )
            messages.append({
                "role": "system",
                "content": rendered,
                "id": generate_message_id_func("m_sys_sub"),
            })

        messages.append(user_message)
        child_state.messages = messages.copy()
        return messages

    def _build_child_ctx(self, tool_name: str) -> Any:
        """Построить RunContext для дочернего агента (без субагентов в tools)."""
        child_config = self._build_child_config()
        child_run_id = generate_run_id()
        child_agent_kind = tool_name
        child_logger, child_log_path, child_summary_trace_path = create_run_logger(
            child_config, run_id=child_run_id, agent_kind=child_agent_kind
        )

        parent_workspace = getattr(self._ctx, "workspace", None)
        if not isinstance(parent_workspace, ExecutionWorkspace):
            raise RuntimeError(
                "SubagentRunner: parent ctx.workspace is missing; "
                "create_run_context() must assemble ExecutionWorkspace"
            )
        parent_backend = getattr(self._ctx, "os_backend", None) or parent_workspace.backend
        child_skills_registry = get_skills_registry(
            child_config, backend=parent_backend
        )
        child_tools_data, child_tools_spec, child_tools_descr, _mcp_status = build_tools_data(
            child_config,
            os_backend=parent_backend,
            skills_registry=child_skills_registry,
            logger=child_logger,
        )

        child_ctx = RunContext(
            config_dict=child_config,
            run_id=child_run_id,
            # config_dict наследуется дословно через child_config, но поле ctx
            # собирается руками и НЕ наследуется автоматически — без этой строки
            # ctx.trace_id ребёнка разъедется с его же config_dict.
            trace_id=trace_id_from_config(child_config),
            auth_connector=getattr(self._ctx, "auth_connector", None),
            logger=child_logger,
            logging_path=child_log_path,
            summary_trace_path=child_summary_trace_path,
            chat_completions_llm=chat_completions_llm_from_config_dict(child_config),
            intent_scope=intent_scope_from_config_dict(child_config),
            tools_data=child_tools_data,
            tools_spec=child_tools_spec,
            tools_description=child_tools_descr,
            os_backend=parent_backend,
            skills_registry=child_skills_registry,
            workspace=parent_workspace,
            environment=getattr(self._ctx, "environment", None),
            project_gc_dir=getattr(self._ctx, "project_gc_dir", Path(".")),
        )
        # Режим родителя (C3 gc.force_collect default on CLI/TUI) — наследуем,
        # иначе child process_tool_calls видит оба флага False и collect выключается.
        child_ctx.is_tui = bool(getattr(self._ctx, "is_tui", False))
        child_ctx.is_cli_mode = bool(getattr(self._ctx, "is_cli_mode", False))
        # Progress bridge: same sink as parent (TUI queue or None/no-op); call_id of
        # the parent subagent_* function_call for correlating nested UI rows.
        child_ctx.subagent_progress_sink = getattr(
            self._ctx, "subagent_progress_sink", None
        )
        child_ctx.parent_call_id = self._parent_call_id
        # Sidecar identity: parent thread path (independent of sink). No thread → clear.
        parent_threads_dir = getattr(self._ctx, "threads_dir", None)
        parent_system_id = getattr(self._ctx, "system_id", None)
        parent_thread_id = getattr(self._ctx, "thread_id", None)
        if (
            isinstance(parent_threads_dir, str)
            and parent_threads_dir.strip()
            and isinstance(parent_system_id, str)
            and parent_system_id.strip()
            and isinstance(parent_thread_id, str)
            and parent_thread_id.strip()
        ):
            bind_thread_identity(
                child_ctx,
                threads_dir=parent_threads_dir,
                system_id=parent_system_id,
                thread_id=parent_thread_id,
            )
        else:
            clear_thread_identity(child_ctx)
        # HITL: тот же confirm-баннер родителя + имя субагента для метки via.
        child_ctx.tui_confirm = getattr(self._ctx, "tui_confirm", None)
        child_ctx.permission_store = getattr(self._ctx, "permission_store", None)
        child_ctx.subagent_tool_name = tool_name
        return child_ctx

    def _success_response(
        self,
        tool_name: str,
        result: Any,
        steps: int,
        finish_reason: str,
        usage: Dict[str, Any] | None = None,
        child_run_id: str = "",
    ) -> str:
        meta: Dict[str, Any] = {
            "tool_name": tool_name,
            "steps": steps,
            "finish_reason": finish_reason,
        }
        if child_run_id:
            meta["child_run_id"] = child_run_id
        if usage is not None:
            meta["usage"] = usage
        payload = {
            "ok": True,
            "meta": meta,
            "result": result,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _cancelled_by_user_response(
        self,
        tool_name: str,
        steps: int,
        child_run_id: str = "",
    ) -> str:
        """Envelope A: active subagent interrupted by user stop."""
        meta: Dict[str, Any] = {
            "tool_name": tool_name,
            "steps": steps,
            "finish_reason": "stopped_by_user",
        }
        if child_run_id:
            meta["child_run_id"] = child_run_id
        return cancelled_by_user_output(tool_name, meta=meta)

    def _error_response(
        self,
        tool_name: str,
        code: str,
        message: str,
        retryable: bool,
        steps: int = 0,
        child_run_id: str = "",
    ) -> str:
        meta: Dict[str, Any] = {
            "tool_name": tool_name,
            "steps": steps,
            "finish_reason": "error",
        }
        if child_run_id:
            meta["child_run_id"] = child_run_id
        payload = {
            "ok": False,
            "meta": meta,
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
            },
        }
        return json.dumps(payload, ensure_ascii=False)

    def _log_lifecycle(
        self,
        event: str,
        tool_name: str,
        child_state: Optional[State] = None,
        child_ctx: Optional[Any] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "event": event,
            "tool_name": tool_name,
            "parent_state_id": self._orchestrator_state.state_id,
            "parent_run_id": getattr(self._ctx, "run_id", None),
        }
        if child_state is not None:
            payload["child_state_id"] = child_state.state_id
        if child_ctx is not None:
            payload["child_run_id"] = getattr(child_ctx, "run_id", None)
        payload["subagent_tool_name"] = tool_name
        if isinstance(extra, dict):
            payload.update(extra)

        parent_logger = getattr(self._ctx, "logger", None)
        if isinstance(parent_logger, logging.Logger):
            parent_logger.info("subagent_lifecycle %s", json.dumps(payload, ensure_ascii=False))
        child_logger = getattr(child_ctx, "logger", None) if child_ctx is not None else None
        if isinstance(child_logger, logging.Logger):
            child_logger.info("subagent_lifecycle %s", json.dumps(payload, ensure_ascii=False))
