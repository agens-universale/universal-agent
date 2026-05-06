import asyncio
import json
import logging
import os
from functools import partial
from typing import Any, Callable, Dict, List, Optional

from summarize.compaction import (
    history_exceeds_summary_trigger,
    run_summary_compaction,
)
from config_modules.stored_outputs import get_stored_tool_outputs_max_entries
from summarize.config import parse_summary_trigger_chars
from states import State, save_state
from tools import build_tools_data
from tools.subagents.registry import get_subagent_class
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func
from utils.local_render_system_prompt import render_system_prompt
from utils.logging_module import generate_run_id

_log = logging.getLogger(__name__)


class SubagentRunner:
    """Создаёт дочерний State и запускает run_agent для субагента."""

    def __init__(self, ctx: Any, orchestrator_state: State, config_dict: Dict[str, Any]):
        self._ctx = ctx
        self._orchestrator_state = orchestrator_state
        self._config = config_dict

    def run(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        run_agent_func: Callable,
    ) -> str:
        subagent_cls = get_subagent_class(tool_name)
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
            instance = asyncio.run(
                subagent_cls.create_instance(
                    task=task, model=model, config_dict=self._config, **extra,
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
        save_state(child_state)

        messages = self._build_initial_messages(child_state, agent_role, initial_message)

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

        persist_messages_callback = None
        pre_backend_compaction_hook = None
        if should_enable_summary:
            persist_messages_callback = partial(
                self._persist_runtime_step, child_state
            )
            pre_backend_compaction_hook = partial(
                self._run_pre_backend_summary,
                tool_name,
                child_ctx,
                child_state,
                summary_trigger_chars,
            )

        try:
            result = run_agent_func(
                messages,
                child_state,
                child_ctx,
                check_stop=self._check_stop_requested,
                persist_messages_callback=persist_messages_callback,
                pre_backend_compaction_hook=pre_backend_compaction_hook,
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
            return self._error_response(
                tool_name=tool_name,
                code="subagent_run_failed",
                message=f"Subagent {tool_name} run failed: {exc}",
                retryable=True,
                steps=child_state.steps,
            )

        if result is None:
            self._log_lifecycle(
                event="subagent_end",
                tool_name=tool_name,
                child_state=child_state,
                child_ctx=child_ctx,
                extra={"steps": child_state.steps, "finish_reason": "stopped_by_user"},
            )
            return self._error_response(
                tool_name=tool_name,
                code="subagent_stopped_by_user",
                message=f"Subagent {tool_name} stopped by user request. Run it again if needed.",
                retryable=True,
                steps=child_state.steps,
            )

        result_payload: Any = result or "Subagent finished without explicit result."
        self._log_lifecycle(
            event="subagent_end",
            tool_name=tool_name,
            child_state=child_state,
            child_ctx=child_ctx,
            extra={"steps": child_state.steps, "finish_reason": "completed"},
        )
        return self._success_response(
            tool_name=tool_name,
            result=result_payload,
            steps=child_state.steps,
            finish_reason="completed",
        )

    def _check_stop_requested(self) -> bool:
        return self._orchestrator_state.stop_requested

    def _persist_runtime_step(
        self, child_state: State, step_messages: List[Dict[str, Any]]
    ) -> None:
        child_state.append_user_messages_to_log(step_messages)

    def _current_backend_event_batch(
        self, current_messages: List[Dict[str, Any]], mode: str
    ) -> List[Dict[str, Any]]:
        if not current_messages:
            return []
        if mode == "user_ask":
            return [current_messages[-1]]
        if mode == "function_call_output":
            return [m for m in current_messages if isinstance(m, dict)]
        return []

    def _run_pre_backend_summary(
        self,
        tool_name: str,
        child_ctx: Any,
        child_state: State,
        summary_trigger_chars: int,
        current_messages: List[Dict[str, Any]],
        mode: str,
    ) -> None:
        try:
            if not history_exceeds_summary_trigger(
                state=child_state,
                summary_trigger_chars=summary_trigger_chars,
            ):
                return
            preserved_batch = self._current_backend_event_batch(current_messages, mode)
            if preserved_batch:
                state_messages = list(getattr(child_state, "messages", []) or [])
                if (
                    len(state_messages) >= len(preserved_batch)
                    and state_messages[-len(preserved_batch):] == preserved_batch
                ):
                    child_state.messages = state_messages[:-len(preserved_batch)]
            try:
                run_summary_compaction(
                    ctx=child_ctx,
                    state=child_state,
                    trigger_reason="subagent_pre_backend",
                    is_cli_mode=False,
                )
            finally:
                child_state.messages += preserved_batch
            if preserved_batch:
                current_messages[:] = preserved_batch
        except Exception:
            _log.warning(
                "Subagent %s pre-backend summary failed",
                tool_name,
                exc_info=True,
            )

    def _build_child_state(self, agent_role: str) -> State:
        """Создать дочерний State, наследуя общие данные от оркестратора."""
        child_config = {**self._config, "use_local_subagents": False}
        child_tools_data, child_tools_spec, child_tools_descr = build_tools_data(child_config)
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
        )
        child_state.tool_call_outputs_store.set_max_entries(
            get_stored_tool_outputs_max_entries(child_config)
        )
        child_state.backend_api_mode = "chat_completions"
        return child_state

    def _resolve_subagent_system_prompt(self) -> Optional[str]:
        """Вернуть системный промпт для субагента с fallback на промпт родителя."""
        prompt_path_raw = self._config.get("chat_completions_subagent_system_prompt_path")
        candidate_paths: List[str] = []
        if isinstance(prompt_path_raw, str) and prompt_path_raw.strip():
            candidate_paths.append(os.path.abspath(prompt_path_raw.strip()))
        base_dir = os.path.dirname(os.path.abspath(__file__))
        default_prompt_path = os.path.abspath(
            os.path.join(
                base_dir,
                "..",
                "..",
                "..",
                "prompts",
                "chat_completions_subagent_system_prompt.md",
            )
        )
        candidate_paths.append(default_prompt_path)

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
        self, child_state: State, agent_role: str, initial_user_message: str,
    ) -> List[Dict[str, Any]]:
        """Сформировать начальные сообщения (system + user) и записать их в child_state."""
        messages: List[Dict[str, Any]] = []

        system_prompt = child_state.chat_completions_system_prompt
        if system_prompt:
            rendered = render_system_prompt(
                system_prompt=system_prompt,
                tools_spec=child_state.tools,
                project_info=child_state.initial_project_data,
                specific_role=agent_role,
            )
            messages.append({
                "role": "system",
                "content": rendered,
                "id": generate_message_id_func("m_sys_sub"),
            })

        messages.append({
            "role": "user",
            "content": initial_user_message,
            "id": generate_message_id_func("m_user_sub"),
        })

        child_state.messages = messages.copy()
        return messages

    def _build_child_ctx(self, tool_name: str) -> Any:
        """Построить RunContext для дочернего агента (без субагентов в tools)."""
        from run_context import RunContext
        from utils.logging_module import create_run_logger

        child_config = {**self._config, "use_local_subagents": False}
        child_run_id = generate_run_id()
        child_agent_kind = tool_name
        child_tools_data, child_tools_spec, child_tools_descr = build_tools_data(
            child_config
        )
        child_logger, child_log_path, child_summary_trace_path = create_run_logger(
            child_config, run_id=child_run_id, agent_kind=child_agent_kind
        )

        return RunContext(
            config_dict=child_config,
            run_id=child_run_id,
            auth_connector=getattr(self._ctx, "auth_connector", None),
            logger=child_logger,
            logging_path=child_log_path,
            summary_trace_path=child_summary_trace_path,
            tools_data=child_tools_data,
            tools_spec=child_tools_spec,
            tools_description=child_tools_descr,
        )

    def _success_response(
        self, tool_name: str, result: Any, steps: int, finish_reason: str,
    ) -> str:
        payload = {
            "ok": True,
            "meta": {
                "tool_name": tool_name,
                "steps": steps,
                "finish_reason": finish_reason,
            },
            "result": result,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _error_response(
        self,
        tool_name: str,
        code: str,
        message: str,
        retryable: bool,
        steps: int = 0,
    ) -> str:
        payload = {
            "ok": False,
            "meta": {
                "tool_name": tool_name,
                "steps": steps,
                "finish_reason": "error",
            },
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
