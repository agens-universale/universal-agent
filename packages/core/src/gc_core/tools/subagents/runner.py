import asyncio
import copy
import inspect
import json
import logging
import os
import threading
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from gc_core.bundled_assets import bundled_root
from gc_core.trace_id import trace_id_from_config
from gc_core.config_modules.stored_outputs import get_stored_tool_outputs_max_entries
from gc_core.config_modules.chat_completions import auxiliary_llm_from_config_dict
from gc_core.config_modules.intent_scope import intent_scope_from_config_dict
from gc_core.execution_workspace import ExecutionWorkspace
from gc_core.model_router import AliasCircuitOpenError, ModelRouter, SUBAGENT_CONSUMER_PREFIX
from gc_core.runtime_notices import maybe_push_fallback_to_active_notice
from gc_core.run_summary_coordinator import RunSummaryCoordinator
from gc_core.summarize.config import parse_summary_trigger_chars
from gc_core.constants.tool_names import SESSION_MEMORY_TOOL_NAME
from gc_core.message_policy.tool_availability import available_tool_names, tool_is_available
from gc_core.message_policy.tool_usage_notice.policy import get_thread_memory_registers_from_shared, subagent_thread_memory_bootstrap_text, wrap_thread_memory_technical_user_message
from gc_core.message_policy.tool_usage_notice.state import persist_tool_usage_notice_state
from gc_core.intent_log.user_turn import append_user_asks_from_messages, init_subagent_open_turn
from gc_core.run_context import RunContext
from gc_core.tools.skills.registry import get_skills_registry
from gc_core.states import State
from gc_core.tools import SUBAGENT_TOOLS_ALLOW_KEY, SUBAGENT_TOOLS_STOP_KEY, abuild_tools_data
from gc_core.tools.subagents.registry import get_subagent_class, resolve_subagent_definitions
from gc_core.turn_boundary import begin_turn
from gc_core.usage.account import CATEGORY_SUBAGENT as USAGE_CATEGORY_SUBAGENT
from gc_core.usage.account import UsageAccount
from gc_core.usage.context import usage_scope
from gc_core.config_modules.agent import merge_special_instructions_with_model_steering
from gc_core.utils.backend_errors import is_model_unavailable_error
from gc_core.utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func
from gc_core.utils.local_render_system_prompt import render_system_prompt
from gc_core.utils.logging_module import (
    create_run_logger,
    generate_run_id,
    release_run_logger,
)
from gc_core.message_policy.user_message_policy import extract_real_user_message_text
from gc_core.message_policy.user_stop import cancelled_by_user_output
from gc_core.tools.subagents.active_scope import activate_child_input
from gc_core.tools.subagents.progress import emit_progress
from gc_core.tools.subagents.sidecar import (
    apersist_subagent_trace,
    bind_thread_identity,
    clear_thread_identity,
)
from gc_core.utils.aio_compat import await_callback

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


# Вложенные узлы config_dict, передаваемые по ссылке при сборке дочернего конфига
# (спека §7 п.4): заголовки/kwargs backend + реестр моделей Router (models/slots/
# assign — Router их не мутирует, но `{**self._config}` расшарил бы эти dict'ы с
# родителем; deepcopy только их, не всего конфига — остальные секции read-only).
_CHILD_CONFIG_DEEP_COPY_KEYS = (
    "chat_completion_headers",
    "chat_completion_kwargs",
    "auxiliary_llm_params",
    "models",
    "slots",
    "assign",
)


async def _drain_trace_writes(futures: List[Any], lock: "threading.Lock") -> None:
    """Дождаться записей трассы, поставленных в очередь из чужого потока.

    Без этого прогон возвращался бы раньше своих же записей, и `asyncio.run`
    вызывающего снимал бы их отменой — трасса терялась бы молча, тем тише чем
    позже пришло сообщение. Отказ каждой записи логируется отдельно: возврат
    ``apersist_subagent_trace`` не читает ни один вызывающий, и другого следа
    у ошибки нет.

    Цикл, а не один снимок. Первая редакция снимала список ОДИН раз, и этого
    мало по двум причинам: колбэк всё ещё вооружён (его снимает вызывающий
    перед дренажом), а чужой поток мог добавить future между снимком и
    `clear()`. Выходим, когда список пуст, — тогда добавлять больше некому.

    Про отмену. Если нас отменили, каждое следующее ожидание бросит сразу, и
    дожидаться нечем: записи уже стоят на ЭТОМ loop и могут доехать сами, а
    могут быть сняты вместе с ним. Гарантии тут нет — поэтому остаток
    пересчитывается и попадает в лог ЧИСЛОМ. Молчаливая потеря — ровно то, от
    чего дренаж и вводился.
    """
    logger = logging.getLogger("gc_core.subagents")
    while True:
        with lock:
            pending = futures[:]
            futures.clear()
        if not pending:
            return
        for index, future in enumerate(pending):
            try:
                await asyncio.wrap_future(future)
            except asyncio.CancelledError:
                left = len(pending) - index
                with lock:
                    left += len(futures)
                logger.warning(
                    "subagent_trace_writes_unconfirmed left=%d: прогон отменён "
                    "во время дренажа; записи поставлены на loop, но их "
                    "завершение не подтверждено",
                    left,
                )
                raise
            except Exception:
                logger.warning("subagent_trace_write_failed", exc_info=True)


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

    async def arun(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        run_agent_func: Callable,
        *,
        parent_call_id: Optional[str] = None,
    ) -> str:
        """Прогон субагента. ЕДИНСТВЕННАЯ форма.

        Асинхронный, потому что внутри — полноценный прогон агента: держать на
        нём поток означало бы, что переезд ядра на asyncio останавливается на
        границе субагентов, а они самые долгие.

        Синхронные близнецы (``run``, ``_run_internal``) сняты. Оба были
        обёртками над ``run_coro_blocking``, то есть заводили ВТОРОЙ loop, а из
        под живого — морозили поток вызывающего на весь прогон субагента.
        Производственных вызывающих у них не было ни одного: ``apply_tool``
        ходит корутиной, остальные 48 вызовов жили в тестах.
        """
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
        return await self._arun_internal(tool_name, subagent_cls, arguments, run_agent_func)

    async def _arun_internal(
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
            # Прямой await, а не run_coro_blocking: тело уже асинхронное. Хелпер
            # при живом loop блокирует поток этого самого loop, а заодно теряет
            # contextvars — usage_scope ниже не доходил бы до вложенных вызовов.
            instance = await subagent_cls.create_instance(
                task=task,
                model=model,
                config_dict=self._config,
                runtime_notices_enabled=bool(getattr(self._ctx, "is_tui", False)),
                **extra,
            )
        except Exception as exc:
            return self._error_response(
                tool_name=tool_name,
                code="subagent_create_failed",
                message=f"Subagent {tool_name} create_instance failed: {exc}",
                retryable=True,
            )

        try:
            child_ctx = await self._build_child_ctx(tool_name=tool_name)
        except Exception as exc:
            return self._error_response(
                tool_name=tool_name,
                code="subagent_ctx_build_failed",
                message=f"Subagent {tool_name} child context build failed: {exc}",
                retryable=True,
            )

        primary_child_logger = getattr(child_ctx, "logger", None)
        try:
            try:
                # finalize_role некоторых субагентов (special_agent) делает LLM-вызов
                # генерации роли — относим его токены к категории subagent родителя.
                with usage_scope(self._orchestrator_state.usage, USAGE_CATEGORY_SUBAGENT):
                    await instance.afinalize_role(child_ctx, self._orchestrator_state)
            except Exception as exc:
                return self._error_response(
                    tool_name=tool_name,
                    code="subagent_role_generation_failed",
                    message=f"Subagent {tool_name} role generation failed: {exc}",
                    retryable=True,
                )
            agent_role, initial_message = instance.get_initial_data()

            try:
                child_state = await self._build_child_state(agent_role)
            except Exception as exc:
                return self._error_response(
                    tool_name=tool_name,
                    code="subagent_state_build_failed",
                    message=f"Subagent {tool_name} child state build failed: {exc}",
                    retryable=False,
                )
            # child_ctx.model_router — свой Router ребёнка (спека §7 п.4): фиксирует
            # default_model на модель субагента, как begin_turn фиксирует его на
            # активную модель в обычном ходе (turn_boundary.begin_turn).
            begin_turn(child_state, child_ctx)


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

                async def _on_summary_applied_with_sidecar(event: Dict[str, Any]) -> None:
                    # `on_summary_applied` — корутина. Без `await` она молча не
                    # исполнялась: терялись `append_summary_trace_record` и
                    # `archive_suffix`. Прогон это ВИДЕЛ — `RuntimeWarning:
                    # coroutine ... was never awaited`, — но предупреждение не
                    # роняет набор, и тест был зелёным, потому что проверяет
                    # только строку sidecar. `await_callback`, а не голый `await`:
                    # у координатора встраивающего колбэк может быть и обычной
                    # функцией.
                    await await_callback(_orig_on_summary(event))
                    synthesis = _synthesis_from_summary_event(event)
                    await apersist_subagent_trace(
                        child_ctx,
                        kind="child_summary",
                        payload={"synthesis": synthesis},
                    )

                summary.on_summary_applied = _on_summary_applied_with_sidecar
                # Корутинный двойник, как и у родителя (agent_executor).
                # Корутинным именем: синхронное fail-open внутри проглотил бы,
                # и компакция субагента молча
                # переставала срабатывать.
                pre_backend_compaction_hook = summary.apre_backend_compaction_hook

            self._usage_pushed = UsageAccount()
            self._tool_calls_pushed = 0
            try:
                    result = await self._arun_child_loop(
                        messages=messages,
                    child_state=child_state,
                    child_ctx=child_ctx,
                    run_agent_func=run_agent_func,
                    persist_messages_callback=persist_messages_callback,
                    pre_backend_compaction_hook=pre_backend_compaction_hook,
                    child_meta=child_meta,
                )
            except Exception as exc:
                fallback_ctx = self._maybe_pin_fallback(
                    child_ctx=child_ctx, child_state=child_state, exc=exc, tool_name=tool_name
                )
                if fallback_ctx is None:
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
                    await self._persist_finished(
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
                # Класс 1 — субагент с пином (спека §8): фейл на первом вызове ребёнка →
                # рестарт с исходных сообщений (трейс пуст, ничего не потеряно); фейл не
                # на первом — деградация с текущего шага (child_state.messages уже несёт
                # батч, на котором упал вызов). Повторный фейл — честная ошибка тула,
                # без ещё одного фоллбэка (fallback_ctx строится максимум один раз).
                child_ctx = fallback_ctx
                retry_messages = messages if child_state.steps == 0 else child_state.messages
                try:
                    result = await self._arun_child_loop(
                        messages=retry_messages,
                        child_state=child_state,
                        child_ctx=child_ctx,
                        run_agent_func=run_agent_func,
                        persist_messages_callback=persist_messages_callback,
                        pre_backend_compaction_hook=pre_backend_compaction_hook,
                        child_meta=child_meta,
                    )
                except Exception as retry_exc:
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
                    await self._persist_finished(
                        child_ctx,
                        child_state,
                        ok=False,
                        finish_reason="error",
                    )
                    return self._error_response(
                        tool_name=tool_name,
                        code="subagent_run_failed",
                        message=f"Subagent {tool_name} run failed: {retry_exc}",
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
                await self._persist_finished(
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
                await self._persist_finished(
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
            await self._persist_finished(
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
        finally:
            # Логгер субагента уникален по имени и живёт в реестре `logging` до
            # конца процесса вместе с файловым хендлером — освобождать надо.
            # Но ПОСЛЕ всех записей о завершении: раньше это делал внутренний
            # finally, до `_log_lifecycle(event="subagent_end")`, и запись о
            # завершении в логе субагента терялась. Здесь же оба логгера, если
            # прогон ушёл на запасной контекст (первый раньше утекал), и ранние
            # выходы после сборки контекста.
            final_child_logger = getattr(child_ctx, "logger", None)
            release_run_logger(primary_child_logger)
            if final_child_logger is not primary_child_logger:
                release_run_logger(final_child_logger)

    async def _arun_child_loop(
        self,
        *,
        messages: List[Dict[str, Any]],
        child_state: State,
        child_ctx: Any,
        run_agent_func: Callable,
        persist_messages_callback: Callable,
        pre_backend_compaction_hook: Optional[Callable],
        child_meta: Dict[str, Any],
    ) -> Any:
        """Один прогон дочернего цикла внутри своего ``activate_child_input`` scope.
        Вынесено отдельно, чтобы класс-1 фоллбэк (спека §8, ``_maybe_pin_fallback``)
        мог вызвать его повторно (рестарт/деградация) со свежим scope — переиспользовать
        уже закрытый ``scope`` первой попытки небезопасно (его `__exit__` уже отработал)."""
        with activate_child_input(
            self._ctx, parent_call_id=self._parent_call_id
        ) as scope:
            # Колбэк enqueue приходит из ЧУЖОГО потока: его дёргает
            # `AgentExecutor.add_message`, которую зовёт TUI, и loop'а там нет.
            # Поэтому запись трассы уезжает на loop ЭТОГО прогона, а не делается
            # на месте. Синхронной формы у операции больше нет, и это к лучшему:
            # прежняя ОТКАЗЫВАЛАСЬ писать в асинхронное хранилище (лог-варнинг и
            # `return None`), то есть у встраивающего с redis/postgres история
            # вызовов субагента не писалась вовсе, а сами субагенты работали —
            # заметить было нечего.
            loop = asyncio.get_running_loop()
            # Планировать и забыть нельзя: `asyncio.run` вызывающего снимает
            # незавершённые задачи ОТМЕНОЙ при закрытии loop, и запись,
            # поставленную в очередь последним сообщением, теряло молча
            # (проверено: CancelledError в done-колбэке). Поэтому futures
            # собираются и дожидаются на границе прогона. Лок — потому что
            # добавляет их чужой поток, а разбирает loop.
            trace_writes: list = []
            trace_writes_lock = threading.Lock()

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
                future = asyncio.run_coroutine_threadsafe(
                    apersist_subagent_trace(
                        child_ctx, kind="user", payload=payload
                    ),
                    loop,
                )
                with trace_writes_lock:
                    trace_writes.append(future)

            scope.set_on_enqueue(_on_child_user_enqueue)
            # run_agent_func обязана быть корутинной — это arun_agent. Раньше здесь
            # стоял _maybe_await «на случай синхронной заглушки»; он же и прятал
            # поломку: TypeError от await над обычной функцией ловил except ниже и
            # превращал в конверт subagent_run_failed, а тесты, не проверяющие
            # результат, оставались зелёными.
            try:
                return await run_agent_func(
                    messages,
                    child_state,
                    child_ctx,
                    check_stop=self._check_stop_requested,
                    persist_messages_callback=persist_messages_callback,
                    pre_backend_compaction_hook=pre_backend_compaction_hook,
                    session_meta=child_meta,
                    get_pending_messages=scope.drain,
                )
            finally:
                # Колбэк снимается ПЕРВЫМ делом: пока он вооружён, чужой поток
                # добавляет futures, и дренаж гонялся бы за собственным хвостом.
                scope.set_on_enqueue(None)
                await _drain_trace_writes(trace_writes, trace_writes_lock)

    def _maybe_pin_fallback(
        self,
        *,
        child_ctx: Any,
        child_state: State,
        exc: Exception,
        tool_name: str,
    ) -> Optional[Any]:
        """Класс 1 — субагент (пинованный или нет, спека §8): фейл подтверждённо
        недоступной модели → пере-резолв ребёнка на активную модель. Возвращает
        деградированный ``child_ctx`` (клон Router'а с ``unbound_override`` =
        активная модель) — вызывающий код решает сам, рестартовать (steps == 0)
        или продолжить с текущего шага. ``None`` — фейл не по этой ветке (не
        недоступность модели / ребёнок не субагент / деградировать некуда) —
        вызывающий код идёт в честную ошибку тула как раньше.

        Предохранитель ставится на упавший алиас ВСЕГДА при подтверждённой
        недоступности — включая случай «деградировать некуда» (пин совпадает с
        активной моделью, или это вовсе непинованный субагент): иначе ни один
        trip-путь не срабатывает для непинованных субагентов, и повторные вызовы
        продолжают платить полный backoff (регресс §8: «не долбим мёртвый
        endpoint», «ВСЕ резолвы мгновенно»)."""
        router = getattr(child_ctx, "model_router", None)
        if router is None or not getattr(router, "is_subagent_child", False):
            return None
        if not is_model_unavailable_error(exc):
            return None
        parent_router = getattr(self._ctx, "model_router", None)
        if parent_router is None:
            return None

        try:
            failed_resolved = router.resolve("main")
        except AliasCircuitOpenError as open_exc:
            failed_resolved = open_exc.resolved
        if failed_resolved.alias is None:
            return None

        # Активную модель резолвим ДО trip() ниже: иначе, если это непинованный
        # субагент (override == активная модель), повторный резолв "main" на тот
        # же алиас упал бы AliasCircuitOpenError раньше сравнения.
        active_already_open = False
        try:
            active_resolved = parent_router.resolve("main")
        except AliasCircuitOpenError as open_exc:
            active_resolved = open_exc.resolved
            active_already_open = True

        # Фейл подтверждён — предохранитель встаёт независимо от того, есть ли
        # куда деградировать (см. докстроку выше).
        router.breaker.trip(
            failed_resolved.alias, str(exc).strip()[:300] or exc.__class__.__name__
        )

        if active_already_open or active_resolved.alias == failed_resolved.alias:
            # Деградировать некуда: активная модель уже сама в предохранителе,
            # либо она и есть упавшая (пин == активная / субагент без пина
            # резолвился в активную) — то же самое упадёт снова. Честная ошибка
            # тула ниже; предохранитель уже открыт — следующий вызов (этого
            # субагента или main) отвалится мгновенно.
            return None

        kind = "рестарт" if child_state.steps == 0 else "деградация"
        who = f"субагент {tool_name}"
        logger = getattr(self._ctx, "logger", None)
        message = (
            f"{who} ({kind}): {failed_resolved.alias} недоступна — "
            f"выполнено активной {active_resolved.model}"
        )
        if logger is not None:
            logger.warning(message)
        maybe_push_fallback_to_active_notice(
            who=who,
            failed_alias=failed_resolved.alias,
            active_model=active_resolved.model,
            mode=kind,
            enabled=bool(getattr(self._ctx, "is_tui", False)),
        )
        child_ctx.model_router = router.with_override(active_resolved)
        begin_turn(child_state, child_ctx)
        return child_ctx

    def _check_stop_requested(self) -> bool:
        return self._orchestrator_state.stop_requested

    async def _persist_finished(
        self,
        child_ctx: Any,
        child_state: State,
        *,
        ok: bool,
        finish_reason: str,
    ) -> None:
        await apersist_subagent_trace(
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

        Узлы из ``_CHILD_CONFIG_DEEP_COPY_KEYS`` (заголовки/kwargs backend +
        реестр моделей Router — models/slots/assign) копируются deepcopy, а не
        по ссылке: иначе мутация ребёнка (например, override child-слота в
        ``_build_child_model_router``) утекла бы в ``self._config`` и оттуда —
        во все последующие ходы/субагентов сессии.
        """
        child_config = {**self._config, "use_local_subagents": False}
        for key in _CHILD_CONFIG_DEEP_COPY_KEYS:
            if key in child_config:
                child_config[key] = copy.deepcopy(child_config[key])
        definition = resolve_subagent_definitions(self._config).get(
            self._active_tool_name
        )
        if definition is not None:
            child_config[SUBAGENT_TOOLS_ALLOW_KEY] = (
                list(definition.tools) if definition.tools is not None else None
            )
            child_config[SUBAGENT_TOOLS_STOP_KEY] = list(definition.stop_tools)
        return child_config

    def _build_child_model_router(
        self, tool_name: str, child_config: Dict[str, Any]
    ) -> Optional[ModelRouter]:
        """Свой Router ребёнка (спека §7 п.4): `subagent:<tool_name>` родительского
        Router становится «main» ребёнка и наследуется любым служебным вызовом БЕЗ
        более точной assign-привязки — через `unbound_override` (спека §5.2:
        явные привязки, и уровнем, и алиасом, продолжают резолвиться по
        нетронутым слотам/реестру ребёнка — override их не перекрывает).

        `level_provider` ребёнка — константа (уровень хода родителя на момент
        вызова), не живой TUI-провайдер: используется Router'ом только если
        `unbound_override` не задан (сюда с непустым родительским Router'ом не
        доходим — оставлено для полноты resolve-логики).

        None, если у родителя нет Router (легаси тестовые двойники ctx) — вызывающий
        код и так устойчив к `model_router=None` (turn_boundary/воронки не падают).

        Класс 1 фоллбэков недоступности (спека §8): если пин этого субагента уже
        открыт в предохранителе (прошлый вызов этой же сессии его уронил) — ребёнок
        сразу собирается на активной модели + notice, без повторной попытки на
        заведомо мёртвом пине.

        `force_legacy` родителя (CLI `--model`/`--completions_url` поверх непустого
        реестра, спека §2) наследуется ребёнком (Task 10) — иначе собственная
        assign-привязка ребёнка (например, `finish_gate`) резолвилась бы через
        реальный реестр вместо форс-легаси, расходясь с родителем.
        """
        parent_router = getattr(self._ctx, "model_router", None)
        if parent_router is None:
            return None
        try:
            resolved = parent_router.resolve(f"{SUBAGENT_CONSUMER_PREFIX}{tool_name}")
        except AliasCircuitOpenError as exc:
            try:
                resolved = parent_router.resolve("main")
            except AliasCircuitOpenError as active_exc:
                # И пин субагента, и активная модель в предохранителе — деградировать
                # некуда (симметрично `_maybe_pin_fallback` active_already_open).
                # Честная ошибка тула с внятным сообщением, а не сырой
                # AliasCircuitOpenError, который наверху стал бы subagent_ctx_build_failed
                # с невнятным str.
                raise RuntimeError(
                    f"субагент {tool_name}: пин {exc.alias} и активная модель "
                    f"{active_exc.resolved.model} (алиас {active_exc.alias}) недоступны "
                    f"(предохранитель). Переключите уровень /level или профиль /profiles."
                ) from active_exc
            who = f"субагент {tool_name}"
            logger = getattr(self._ctx, "logger", None)
            message = f"{who}: {exc.alias} недоступна — выполнено активной {resolved.model}"
            if logger is not None:
                logger.warning(message)
            maybe_push_fallback_to_active_notice(
                who=who,
                failed_alias=exc.alias,
                active_model=resolved.model,
                enabled=bool(getattr(self._ctx, "is_tui", False)),
            )
        frozen_level = parent_router.active_level()
        return ModelRouter(
            child_config,
            level_provider=lambda: frozen_level,
            breaker=parent_router.breaker,
            force_legacy=parent_router.force_legacy,
            unbound_override=resolved,
        )

    async def _build_child_state(self, agent_role: str) -> State:
        """Создать дочерний State, наследуя общие данные от оркестратора."""
        child_config = self._build_child_config()
        parent_backend = getattr(self._ctx, "os_backend", None)
        child_tools_data, child_tools_spec, child_tools_descr, _mcp_status = await abuild_tools_data(
            child_config,
            os_backend=parent_backend,
            logger=getattr(self._ctx, "logger", None),
            # Кэш родителя: без него КАЖДЫЙ спавн субагента заново гонит
            # MCP-дискавери, и делает это прямо на event loop (замер: 993 мс
            # заморозки, ~2 с на спавн). Набор mcp-servers у ребёнка тот же —
            # ключ кэша совпадает, попадание гарантировано.
            tools_cache=getattr(self._ctx, "tools_cache", None),
        )
        child_system_prompt = self._resolve_subagent_system_prompt()

        parent = self._orchestrator_state
        child_state = await State.acreate(
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
            # Первый кандидат — путь ИЗ КОНФИГА: файл человека, кодировка его.
            # `UnicodeDecodeError` подкласс `ValueError`, а не `OSError`, и уходил
            # мимо этого фолбэка, роняя запуск сабагента вместо отката на пакетный.
            except (OSError, UnicodeDecodeError):
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

    async def _build_child_ctx(self, tool_name: str) -> Any:
        """Построить RunContext для дочернего агента (без субагентов в tools)."""
        child_config = self._build_child_config()
        child_router = self._build_child_model_router(tool_name, child_config)
        child_run_id = generate_run_id()
        child_agent_kind = tool_name
        child_logger, child_log_path, child_summary_trace_path = create_run_logger(
            child_config, run_id=child_run_id, agent_kind=child_agent_kind
        )

        parent_workspace = getattr(self._ctx, "workspace", None)
        if not isinstance(parent_workspace, ExecutionWorkspace):
            raise RuntimeError(
                "SubagentRunner: parent ctx.workspace is missing; "
                "acreate_run_context() must assemble ExecutionWorkspace"
            )
        parent_backend = getattr(self._ctx, "os_backend", None) or parent_workspace.backend
        child_skills_registry = get_skills_registry(
            child_config, backend=parent_backend
        )
        child_tools_data, child_tools_spec, child_tools_descr, _mcp_status = await abuild_tools_data(
            child_config,
            os_backend=parent_backend,
            skills_registry=child_skills_registry,
            logger=child_logger,
            # Кэш родителя — см. соседний вызов выше: иначе дискавери на каждый
            # спавн, и прямо на event loop.
            tools_cache=getattr(self._ctx, "tools_cache", None),
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
            auxiliary_llm=auxiliary_llm_from_config_dict(child_config),
            intent_scope=intent_scope_from_config_dict(child_config),
            tools_data=child_tools_data,
            tools_spec=child_tools_spec,
            tools_description=child_tools_descr,
            os_backend=parent_backend,
            skills_registry=child_skills_registry,
            workspace=parent_workspace,
            model_router=child_router,
            environment=getattr(self._ctx, "environment", None),
            project_gc_dir=getattr(self._ctx, "project_gc_dir", Path(".")),
            # Кэш едет дальше вниз: иначе внуки (субагент субагента) снова
            # платили бы полную дискавери на каждый спавн.
            tools_cache=getattr(self._ctx, "tools_cache", None),
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
        # Каталог в условие НЕ входит: адрес треда — это (system_id, thread_id),
        # а у стороннего хранилища (база, память) `threads_dir` пуст законно.
        # Прежнее требование уводило такого ребёнка в `clear_thread_identity`, и
        # гейт `resolve_sidecar_identity`, который пустой каталог уже принимает,
        # до решения даже не доходил: трасса субагента молча терялась.
        if (
            isinstance(parent_system_id, str)
            and parent_system_id.strip()
            and isinstance(parent_thread_id, str)
            and parent_thread_id.strip()
        ):
            bind_thread_identity(
                child_ctx,
                threads_dir=(
                    parent_threads_dir if isinstance(parent_threads_dir, str) else ""
                ),
                system_id=parent_system_id,
                thread_id=parent_thread_id,
            )
        else:
            clear_thread_identity(child_ctx)
        # HITL: тот же confirm-баннер родителя + имя субагента для метки via.
        child_ctx.tui_confirm = getattr(self._ctx, "tui_confirm", None)
        child_ctx.permission_store = getattr(self._ctx, "permission_store", None)
        # Порт «спросить человека» — туда же, куда confirm-баннер. Без него ядро
        # читает ответ `input()` в потоке пула, а подмена stdin в TUI
        # потоко-локальна: поток читал настоящую консоль, которую держит
        # Textual, и делил с ним нажатия — ход висел, клавиши пропадали.
        child_ctx.ask_human = getattr(self._ctx, "ask_human", None)
        # Хранилище тредов — туда же, и по той же причине: трассу субагента
        # пишет `apersist_subagent_trace`, а без этого поля она уходила в
        # файловое хранилище по умолчанию. У встраивающего, отдавшего свой стор
        # (`AsyncAgent.create(cfg, thread_store=…)`), трасса либо терялась, либо
        # ложилась на диск хоста мимо него.
        child_ctx.thread_store = getattr(self._ctx, "thread_store", None)
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
