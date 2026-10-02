"""The existing agent loop with explicit bindings to core operations.

This module imports no model clients, tool registry, filesystem or frontends.
The local compatibility entry point supplies its existing helpers, so callers
patching those helpers keep working. Durable runners supply the same policy
and route external operations through their execution port.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from gc_core.execution.contracts import ExecutionFailure
from gc_core.settings import settings_of

if TYPE_CHECKING:
    from gc_core.execution.state import ExecutionState as State
    from gc_core.tools.arguments_validation import (
        WrongFunctionArgumentsException,
    )

_NO_RETURN = object()


class _Outcome:
    """Итог обработки одного режима: новые messages, либо результат прогона, либо рестарт цикла."""

    __slots__ = ("completed_step", "messages", "mode", "restart", "result")

    def __init__(
        self,
        messages=None,
        *,
        result=_NO_RETURN,
        restart=False,
        completed_step=False,
    ):
        self.messages = messages
        self.result = result
        self.restart = restart
        self.completed_step = completed_step
        self.mode = None


class AgentLoop:
    """Главный цикл агента: одна итерация = один режим (user_ask / function_call / …).

    Состояние итераций (счётчик ошибок тулов, ретраи переполнения контекста)
    живёт на объекте; ``arun`` — тот же while-цикл, что был в ``arun_agent``.
    """

    def __init__(
        self,
        state: State,
        ctx: Any,
        *,
        bindings,
        get_full_history_messages,
        persist_messages_callback,
        pre_backend_compaction_hook,
        turn_end_compaction_hook,
        force_compact_hook,
        get_pending_messages,
        check_stop,
        session_meta,
        mark_finalizing,
        mark_finish_gate_checking,
    ) -> None:
        self.bindings = bindings
        self.state = state
        self.ctx = ctx
        self.get_full_history_messages = get_full_history_messages
        self.persist_messages_callback = persist_messages_callback
        self.pre_backend_compaction_hook = pre_backend_compaction_hook
        self.turn_end_compaction_hook = turn_end_compaction_hook
        self.force_compact_hook = force_compact_hook
        self.get_pending_messages = get_pending_messages
        self.check_stop = check_stop
        self.session_meta = session_meta
        self.mark_finalizing = mark_finalizing
        self.mark_finish_gate_checking = mark_finish_gate_checking

        self.tool_error_cnt = 0
        self.context_overflow_retries = 0
        self.run_logger = (
            getattr(ctx, "logger", None) or self.bindings._fallback_logger
        )
        # Плоский вид — только для привязок, которые пока читают его сами
        # (strip_tags_projection, human_console_enabled).
        self.cfg = getattr(ctx, "config_dict", {}) or {}
        agent_cfg = settings_of(ctx).agent
        self.proj_tags = self.bindings.projection_tags_from_config(self.cfg)
        self.console_human = self.bindings.human_console_enabled(self.cfg)
        self.max_tool_errors = agent_cfg.no_tool_call_max_count
        self.session_token_limit = agent_cfg.session_token_limit
        self.max_tool_calls_limit = agent_cfg.max_tool_calls
        self.max_unknown_tool_repeats = int(
            agent_cfg.max_unknown_tool_repeats or 5
        )
        state.last_unknown_tool_name = None
        state.unknown_tool_repeat_count = 0
        state.last_batch_unknown_tool_count = 0
        state.last_batch_invalid_arguments_count = 0

    # ------------------------------------------------------------------ loop
    async def arun(self, messages) -> Union[str, None]:
        state = self.state
        first_poll = True
        while True:
            if self.check_stop is not None and self.check_stop():
                self.bindings.clear_loop_detector(state)
                return None
            messages = self._poll_pending(messages, first_poll=first_poll)
            first_poll = False
            # Если от backend'а или get_pending_messages пришёл пустой список сообщений,
            # не передаём его дальше в check_mode, чтобы не падать
            # с ValueError('Empty messages list'). Вместо этого генерируем понятное
            # сообщение‑обратную связь для агента.
            if not messages:
                messages = self._bounce_empty_messages()
                continue
            step = await self._astep(
                messages, self.bindings._get_backend_api_mode(self.ctx)
            )
            if step.result is not _NO_RETURN:
                return step.result
            messages = step.messages
            if step.restart:
                continue
            self._count_step(step.completed_step, step.mode)

    async def _astep(self, messages, backend_mode: str) -> _Outcome:
        """Одна итерация цикла: подготовка батча и обработка режима."""
        try:
            messages, mode, early = await self._aprepare_batch(
                messages, backend_mode
            )
        except ExecutionFailure:
            raise
        except Exception as e:
            # Ошибки до успешного разбора батча (санитайз, check_mode, …) — bounce.
            return _Outcome(self._bounce_on_error(e), completed_step=True)
        if early is not _NO_RETURN:
            return _Outcome(result=early)
        # Вызовы backend не ловим здесь: после исчерпания transport/adaptive
        # ретраев исключение уходит наверх как есть.
        try:
            outcome = await self._adispatch(mode, messages, backend_mode)
        except self.bindings.WrongFunctionArgumentsException as e:
            # Аргументы отвалились на exec — ход модели всё равно «закрыт».
            outcome = _Outcome(
                self._bounce_wrong_arguments(e), completed_step=True
            )
        except ExecutionFailure:
            raise
        except Exception as backend_exc:
            if not await self._atry_recover_context_overflow(backend_exc):
                raise
            outcome = _Outcome(messages, restart=True)
        outcome.mode = mode
        return outcome

    # ----------------------------------------------------------- iteration
    def _poll_pending(self, messages, *, first_poll: bool):
        """Подхват сообщений из очереди (``get_pending_messages``) перед шагом цикла."""
        if self.get_pending_messages is None:
            return messages
        pending = self.get_pending_messages()
        if not pending:
            return messages
        # На ПЕРВОМ опросе склеиваем со стартовым батчем, а не
        # подменяем его: сообщение, пришедшее между стартом прогона и
        # первым запросом к модели, иначе вытесняло стартовое — и оно
        # не доходило ни до модели, ни до треда. Дальше подмена верна:
        # там `messages` — это уже ответ модели, а не просьба человека.
        return [*messages, *pending] if first_poll else pending

    def _technical_bounce(self, text: str) -> List[Dict]:
        return [
            self.bindings._make_technical_feedback_user_message(
                text,
                technical_type=self.bindings.TECHNICAL_TYPE_TECHNICAL,
            )
        ]

    def _finish_reason_bounce(self) -> List[Dict]:
        """No-tool-call bounce с учётом finish_reason последнего ответа
        (blocked → просьба переформулировать, length → короче, иначе общий)."""
        return self._technical_bounce(
            self.bindings.format_no_tool_call_bounce(
                finish_reason=getattr(self.state, "last_finish_reason", None)
            )
        )

    def _bounce_empty_messages(self) -> List[Dict]:
        self.run_logger.error("Empty messages list")
        self.tool_error_cnt += 1
        self.state.steps += 1
        return self._finish_reason_bounce()

    def _bounce_on_error(self, e: Exception) -> List[Dict]:
        self.run_logger.error(e, exc_info=True)
        self.tool_error_cnt += 1
        return self._finish_reason_bounce()

    def _bounce_wrong_arguments(
        self, e: WrongFunctionArgumentsException
    ) -> List[Dict]:
        self.run_logger.error(e, exc_info=True)
        self.tool_error_cnt += 1
        # Страховка для путей вне батча (finish-аргументы и т.п.): батч
        # свои невалидные вызовы закрывает сам в process_tool_calls.
        self.bindings._attach_truncation_hint(e, self.state)
        return [
            self.bindings._make_technical_feedback_user_message(
                e.to_llm_feedback(),
                technical_type=self.bindings.TECHNICAL_TYPE_ERROR_MESSAGE,
                call_id=getattr(e, "call_id", None),
            )
        ]

    async def _atry_recover_context_overflow(
        self, backend_exc: Exception
    ) -> bool:
        # Переполнение контекстного окна модели → не крашимся: форс-сжатие
        # истории (безусловное, как /compact) и повтор запроса со сжатым
        # контекстом. Лимит попыток — если даже сжатие не помогает, уходим
        # наверх. Прочие ошибки — как раньше, наверх без изменений.
        if not (
            self.force_compact_hook is not None
            and self.bindings.is_context_overflow_error(backend_exc)
            and self.context_overflow_retries
            < self.bindings._MAX_CONTEXT_OVERFLOW_RETRIES
        ):
            return False
        self.context_overflow_retries += 1
        self.run_logger.warning(
            "Context overflow → force-compaction + retry (%s/%s): %s",
            self.context_overflow_retries,
            self.bindings._MAX_CONTEXT_OVERFLOW_RETRIES,
            backend_exc,
        )
        await self.bindings._acall_blocking_hook(self.force_compact_hook)
        return True

    def _count_step(
        self, count_completed_step: bool, mode_for_step_count
    ) -> None:
        # step += 1 только после завершённого хода модели (tool-batch / text /
        # bounce / pre-mode error). user_ask и function_call_output — half-turn
        # к LLM, step не тратят. skip_initial: resume `-t` не считает первый ask.
        state = self.state
        skip_initial_step = bool(
            getattr(state, "skip_initial_user_ask_step_when_history", False)
        )
        skip_this_step = (
            skip_initial_step
            and state.steps == 0
            and mode_for_step_count == "user_ask"
        )
        if count_completed_step and not skip_this_step:
            state.steps += 1

    # ------------------------------------------------------------- prepare
    def _log_incoming(self, messages) -> None:
        for m in messages:
            m_formatted = self.bindings.format_message_responses(m)
            if self.console_human and len(m_formatted) > 0:
                print(m_formatted)
            self.run_logger.info(
                "\n<message>\n%s\n</message>\n",
                self.bindings.json.dumps(m, ensure_ascii=False, indent=2),
            )
            self.run_logger.info(
                "dialog_message",
                extra={
                    "event": "dialog_message",
                    "payload": m,
                    "trace": m_formatted or None,
                },
            )

    def _sanitize_batch(self, messages) -> List[Dict]:
        # В state и в тред уходит уже санитайзнутый набор (битые function_call ->
        # technical user message). Сырой вывод бэкенда логируется выше; если
        # санитайз изменил батч — логируем то, что реально закоммичено (иначе
        # в agent_log не видно подставленного фидбека, хотя он есть в threads jsonl).
        _pre_sanitize_digest = self.bindings.json.dumps(
            messages, ensure_ascii=False, sort_keys=True, default=str
        )
        messages = self.bindings.sanitize_messages_for_chat_completions(
            messages,
            invalid_tool_call_factory=self.bindings.build_invalid_tool_call_feedback_message,
        )
        _post_sanitize_digest = self.bindings.json.dumps(
            messages, ensure_ascii=False, sort_keys=True, default=str
        )
        if _post_sanitize_digest != _pre_sanitize_digest:
            self.run_logger.info(
                "\n<message_batch_committed>\n"
                "Санитайз для chat/completions изменил батч относительно сырого "
                "ответа выше (например, невалидный JSON вызова тула заменён на "
                "техническое user-сообщение — то же, что уходит в persist треда).\n"
                "</message_batch_committed>\n"
            )
            for m in messages:
                self.run_logger.info(
                    "\n<message_committed>\n%s\n</message_committed>\n",
                    self.bindings.json.dumps(
                        m, ensure_ascii=False, indent=2, default=str
                    ),
                )
        return messages

    async def _alimit_envelope(self, messages):
        """Envelope лимита (step / tool_calls / tokens) либо _NO_RETURN."""
        state, ctx = self.state, self.ctx
        # Лимиты — только когда нет pending function_call: сначала дожимаем весь
        # батч tools от модели, иначе call уже в state/jsonl, а exec/sidecar/result
        # теряются. step = завершённый ход модели (после tool-batch / text), не half-turn.
        if self.bindings._batch_has_pending_function_calls(messages):
            return _NO_RETURN
        if state.steps >= state.max_steps:
            state.terminal_outcome = "step_limit"
            return await self.bindings._acall_blocking_hook(
                self.bindings.abuild_limit_envelope, state, "step_limit", ctx
            )
        if (
            self.max_tool_calls_limit is not None
            and state.tool_calls >= self.max_tool_calls_limit
        ):
            state.terminal_outcome = "tool_call_limit"
            return await self.bindings._acall_blocking_hook(
                self.bindings.abuild_limit_envelope,
                state,
                "tool_call_limit",
                ctx,
            )
        if self.session_token_limit is not None:
            _baseline = self.bindings.UsageAccount.from_meta(
                getattr(state, "run_start_usage_meta", None)
            )
            _spent = state.usage.delta_from(_baseline).totals()["total"]
            if _spent >= self.session_token_limit:
                state.terminal_outcome = "token_limit"
                return await self.bindings._acall_blocking_hook(
                    self.bindings.abuild_limit_envelope,
                    state,
                    "token_limit",
                    ctx,
                )
        return _NO_RETURN

    async def _aprepare_batch(self, messages, backend_mode: str):
        """Лог, санитайз, коммит в state/тред, лимиты, режим, pre-backend хуки.

        → (messages, mode, early): ``early`` — результат прогона при достигнутом
        лимите/бюджете ошибок, иначе ``_NO_RETURN``.
        """
        state = self.state
        self._log_incoming(messages)
        messages = self._sanitize_batch(messages)
        new_state_messages = self.bindings.filter_messages_not_in_state(
            list(getattr(state, "messages", []) or []),
            list(messages),
        )
        if new_state_messages:
            state.messages += new_state_messages
        await self.bindings._maybe_persist(
            self.persist_messages_callback, messages, state
        )

        envelope = await self._alimit_envelope(messages)
        if envelope is not _NO_RETURN:
            return messages, None, envelope
        if self.tool_error_cnt > self.max_tool_errors:
            state.terminal_outcome = "tool_errors"
            return messages, None, "Agent tools call errors."

        mode = self.bindings.check_mode(messages, self.run_logger)
        if (
            backend_mode == "chat_completions"
            and self.bindings._mode_requires_backend_call(mode)
        ):
            await self._apre_backend_compaction(messages, mode)
            messages = await self._aapply_tool_usage_notice(messages)
        return messages, mode, _NO_RETURN

    async def _apre_backend_compaction(self, messages, mode) -> None:
        if self.pre_backend_compaction_hook is None:
            return
        try:
            await self.bindings._acall_blocking_hook(
                self.pre_backend_compaction_hook, messages, mode
            )
        except ExecutionFailure:
            raise
        except Exception as pre_backend_exc:
            self.run_logger.warning(
                "Pre-backend compaction hook warning: %s",
                pre_backend_exc,
                exc_info=True,
            )

    async def _aapply_tool_usage_notice(self, messages) -> List[Dict]:
        if not isinstance(self.session_meta, dict):
            return messages
        state = self.state
        try:
            _messages_before_notice = len(messages)
            messages = self.bindings.apply_tool_usage_notice_before_llm(
                messages,
                state,
                self.ctx,
                self.session_meta,
                step_messages=messages,
                logger=self.run_logger,
            )
            messages = await self.bindings.await_callback(messages)
            notice_messages = messages[_messages_before_notice:]
            if notice_messages:
                to_append = self.bindings.filter_messages_not_in_state(
                    list(getattr(state, "messages", []) or []),
                    list(notice_messages),
                )
                if to_append:
                    state.messages += to_append
                await self._apersist_notice_messages(notice_messages)
        except ExecutionFailure:
            raise
        except Exception as mem_exc:
            self.run_logger.warning(
                "Memory messages hook warning: %s",
                mem_exc,
                exc_info=True,
            )
        return messages

    async def _apersist_notice_messages(self, notice_messages) -> None:
        if self.persist_messages_callback is None:
            return
        try:
            await self.bindings.await_callback(
                self.persist_messages_callback(notice_messages)
            )
        except ExecutionFailure:
            raise
        except Exception as persist_exc:
            self.run_logger.warning(
                "Thread notice messages persist warning: %s",
                persist_exc,
                exc_info=True,
            )

    # ------------------------------------------------------------ dispatch
    async def _adispatch(self, mode, messages, backend_mode: str) -> _Outcome:
        if mode == "user_ask":
            return await self._auser_ask(messages, backend_mode)
        if mode == "function_call":
            return await self._afunction_call(messages, backend_mode)
        if mode == "function_call_output":
            return await self._afunction_call_output(messages, backend_mode)
        if mode == "text_response":
            return await self._atext_response(messages)
        raise ValueError(f"unconventional mode: {mode}")

    def _remember_response_id(self, response_id, backend_mode: str) -> None:
        if backend_mode == "responses":
            self.state.previous_response_id = response_id
        self.state.last_backend_response_id = response_id

    async def _auser_ask(self, messages, backend_mode: str) -> _Outcome:
        state, ctx = self.state, self.ctx
        try:
            messages, response_id = await self.bindings.aprocess_user_ask(
                messages, state, ctx
            )
        except ExecutionFailure:
            raise
        except Exception as user_ask_exc:
            can_retry_with_full_history = (
                backend_mode == "responses"
                and state.previous_response_id is not None
                and self.get_full_history_messages is not None
                and self.bindings._is_previous_response_cache_miss(
                    user_ask_exc
                )
            )
            if not can_retry_with_full_history:
                raise

            self.run_logger.warning(
                "Cache miss for previous_response_id=%s. Retry with full history.",
                state.previous_response_id,
            )
            full_history_messages = self.get_full_history_messages()
            state.previous_response_id = None
            (
                messages,
                response_id,
                _,
            ) = await self.bindings._arequest_backend_canonical(
                full_history_messages,
                state,
                ctx,
                set_previous_response_id=False,
            )
        self._remember_response_id(response_id, backend_mode)
        # Half-turn: ответ модели обработаем на следующей итерации.
        return _Outcome(messages)

    async def _afunction_call_output(
        self, messages, backend_mode: str
    ) -> _Outcome:
        messages, response_id = await self.bindings.aprocess_tool_outputs(
            messages, self.state, self.ctx
        )
        self._remember_response_id(response_id, backend_mode)
        # Half-turn: как user_ask — ждём разбора ответа модели.
        return _Outcome(messages)

    async def _afunction_call(self, messages, backend_mode: str) -> _Outcome:
        state = self.state
        pending_tool_calls = self.bindings.group_messages(messages).get(
            "tool_calls", []
        )
        messages = await self.bindings.aprocess_tool_calls(
            messages,
            state,
            self.ctx,
            run_agent_func=self.bindings.arun_agent,
            persist_messages_callback=self.persist_messages_callback,
        )
        closing_finish = self.bindings._closing_finish_after_batch(
            pending_tool_calls,
            messages,
            stop_requested=bool(getattr(state, "stop_requested", False)),
        )
        if closing_finish is not None:
            return await self._afinish_turn(
                messages, closing_finish, backend_mode
            )
        return self._after_tool_batch(messages)

    async def _afinish_turn(
        self, messages, closing_finish, backend_mode: str
    ) -> _Outcome:
        state = self.state
        finish_call_id = closing_finish.get("call_id")
        batch_new = self.bindings.filter_messages_not_in_state(
            list(getattr(state, "messages", []) or []),
            messages,
        )
        work_new, finish_new, tail_new = (
            self.bindings._split_batch_new_around_finish(
                batch_new, finish_call_id
            )
        )
        # Work до гейта; finish в state до гейта (I3: persist после
        # apply). Technical-хвост нужен только при reject, когда
        # агент продолжит ход. После принятого finish его нельзя
        # оставлять в state/jsonl: следующий пользователь иначе
        # увидит устаревший notice как начало нового хода.
        if work_new:
            state.messages += work_new
            await self.bindings._maybe_persist(
                self.persist_messages_callback, work_new, state
            )
        if finish_new:
            # I3: finish output в state до gate; persist — после apply.
            state.messages += finish_new
        finish_arguments = closing_finish.get("arguments")
        if backend_mode != "chat_completions":
            if finish_new:
                await self.bindings._maybe_persist(
                    self.persist_messages_callback, finish_new, state
                )
            state.current_user_intent = []
            state.steps += 1
            self.bindings.clear_loop_detector(state)
            return _Outcome(result=finish_arguments)

        decision = await self._aevaluate_finish_gate(
            finish_arguments, finish_call_id
        )
        if decision.outcome == "reject":
            if tail_new:
                state.messages += tail_new
                await self.bindings._maybe_persist(
                    self.persist_messages_callback, tail_new, state
                )
            # Вердикт уже в output turn_finish (ok: false +
            # message). Не подменяем батч technical-user:
            # следующий шаг цикла сам уйдёт в
            # function_call_output и позовёт модель.
            # Finish-reject — завершённый ход (batch + gate).
            # Work уже в трейсе, не откатываем.
            return _Outcome(messages, completed_step=True)
        # accept / fail_open_error / fail_open_limit / прочее —
        # все завершают ход одинаково.
        state.steps += 1
        state.flush_current_user_intent()
        self.bindings.clear_loop_detector(state)
        return await self._aclose_turn(finish_arguments)

    async def _aevaluate_finish_gate(self, finish_arguments, finish_call_id):
        state, ctx = self.state, self.ctx
        gate = self.bindings.FinishGateGuard(
            ctx=ctx, state=state, logger=self.run_logger
        )
        # Индикатор «сверяюсь…» — только когда судья реально пойдёт
        # (gate.enabled). Smart-skip / strict=off / не-chat → без LLM,
        # флаг не поднимаем (иначе ложный flash + лишний runtime-meta).
        # Снимаем строго в finally, даже если evaluate бросит.
        checking = self.mark_finish_gate_checking is not None and gate.enabled
        if checking:
            await self.bindings.await_callback(
                self.mark_finish_gate_checking(True)
            )
        try:
            decision = await gate.aevaluate_turn_finish(
                finish_handoff_arguments=finish_arguments,
            )
        finally:
            if checking:
                await self.bindings.await_callback(
                    self.mark_finish_gate_checking(False)
                )
        await self.bindings.apply_finish_gate_to_turn_finish_output_in_state(
            state=state,
            finish_call_id=finish_call_id,
            decision=decision,
            logger=self.run_logger,
            persist_messages_callback=self.persist_messages_callback,
        )
        final_submit_output = self.bindings.get_function_call_output_text(
            state.messages, finish_call_id
        )
        await self.bindings._acall_blocking_hook(
            self.bindings.amaybe_update_intent_scope_from_tool,
            tool_name=self.bindings.FINISH_TOOL_NAME,
            tool_output_raw=final_submit_output or "",
            tool_arguments=finish_arguments or "",
            state=state,
            ctx=ctx,
        )
        return decision

    async def _aclose_turn(self, result) -> _Outcome:
        """Конец хода: компакция turn-end и подхват pending-сообщения новым ходом."""
        if self.turn_end_compaction_hook is not None:
            await self.bindings._acall_blocking_hook(
                self.turn_end_compaction_hook
            )
        # Сообщение, пришедшее во время (блокирующей) компакции, не
        # теряем: продолжаем новым ходом на уже сжатом контексте.
        new_turn_messages = self.bindings._pickup_pending_as_new_turn(
            self.get_pending_messages, self.state, self.ctx
        )
        if new_turn_messages is not None:
            return _Outcome(new_turn_messages, restart=True)
        return _Outcome(result=result)

    def _after_tool_batch(self, messages) -> _Outcome:
        state = self.state
        n_unknown = int(
            getattr(state, "last_batch_unknown_tool_count", 0) or 0
        )
        n_invalid = int(
            getattr(state, "last_batch_invalid_arguments_count", 0) or 0
        )
        if n_unknown + n_invalid > 0:
            # Бюджет считаем по ходам без нормального вызова, а не
            # по вызовам: один кривой батч стоит одну единицу, иначе
            # он гасит прогон раньше, чем модель увидит фидбэк.
            self.tool_error_cnt += 1
        else:
            self.tool_error_cnt = 0
        if (
            int(getattr(state, "unknown_tool_repeat_count", 0) or 0)
            >= self.max_unknown_tool_repeats
        ):
            unknown_name = (
                getattr(state, "last_unknown_tool_name", None) or "unknown"
            )
            state.terminal_outcome = "tool_errors"
            return _Outcome(
                result=f"Agent stopped: repeated unknown tool '{unknown_name}'."
            )
        if (
            int(getattr(state, "internal_error_repeat_count", 0) or 0)
            >= self.max_unknown_tool_repeats
        ):
            broken_name = (
                getattr(state, "last_internal_error_tool_name", None)
                or "unknown"
            )
            state.terminal_outcome = "tool_errors"
            return _Outcome(
                result=(
                    f"Agent stopped: tool '{broken_name}' keeps failing with the same client-side error."
                )
            )
        # Полный ход: LLM уже вернул tools, batch исполнен.
        # Невалидный finish сюда же: отказной output в трейсе, ход открыт.
        return _Outcome(messages, completed_step=True)

    async def _atext_decision(
        self, assistant_text: str, last_finish: Any
    ) -> Tuple[bool, Optional[str]]:
        """→ (accept, reject_feedback) для прозы без вызова тула."""
        # blocked / length — не финал даже в allow: отказ провайдера
        # или усечение (обрезок тулы/ответа). Иначе allow принимает
        # мусор и крутит до no_tool_call_max_count / сдаёт ход.
        # Payload уже в state/jsonl; reject → bounce, не discard.
        if self.bindings.finish_prevents_text_final(last_finish):
            return False, None
        decision = await self.bindings.ashould_return_text_answer(
            self.ctx, self.state, assistant_text
        )
        return decision.accept, decision.reject_feedback

    def _text_reject_bounce_msg(
        self,
        reject_feedback: Optional[str],
        last_finish: Any,
        default_msg: str,
    ) -> str:
        """Текст bounce при отклонённой прозе; ``default_msg`` — по finish_reason."""
        if reject_feedback is not None:  # judge-reject c причиной
            return reject_feedback
        if self.bindings.finish_blocks_final(last_finish):
            self.run_logger.warning(
                "Provider blocked the completion (%s); requesting a rephrased attempt",
                last_finish,
            )
            return default_msg
        if (
            self.bindings.classify_finish_kind(last_finish)
            == self.bindings.FINISH_KIND_LENGTH
        ):
            self.run_logger.warning(
                "Agent response truncated (finish_reason=length); requesting a shorter tool call"
            )
            return default_msg
        # deny / пусто / нет гейта / ошибка
        self.run_logger.warning(
            "Agent returned text response without tool call, requesting tool call"
        )
        return self.bindings.NO_TOOL_CALL_BOUNCE_MESSAGE

    async def _atext_response(self, messages) -> _Outcome:
        state, ctx = self.state, self.ctx
        assistant_text = self.bindings.extract_last_assistant_text(
            messages, tags=self.proj_tags, strip_think=True
        )
        last_finish = getattr(state, "last_finish_reason", None)
        bounce_msg = self.bindings.format_no_tool_call_bounce(
            finish_reason=last_finish
        )
        decision_accept, decision_reject_feedback = await self._atext_decision(
            assistant_text, last_finish
        )

        if decision_accept:
            # Финальная проза принята → TUI-индикатор «✓ завершаю…»
            # на время записи/саммари/flush (mark_finalizing пишет
            # runtime.finalizing в meta; снимаем строго в finally).
            state.steps += 1
            if self.mark_finalizing is not None:
                await self.bindings.await_callback(self.mark_finalizing(True))
            try:
                # Корутинным именем, как соседние intent-scope
                # хуки: синхронное имя fail-open внутри проглотил
                # бы, и сводка молча подменилась бы сырым текстом.
                await self.bindings._acall_blocking_hook(
                    self.bindings.amaybe_record_final_answer_intent,
                    assistant_text=assistant_text,
                    state=state,
                    ctx=ctx,
                )
                state.flush_current_user_intent()
                self.bindings.clear_loop_detector(state)
            finally:
                if self.mark_finalizing is not None:
                    await self.bindings.await_callback(
                        self.mark_finalizing(False)
                    )
            return await self._aclose_turn(assistant_text)
        # === reject ===
        self.tool_error_cnt += 1
        bounce_msg = self._text_reject_bounce_msg(
            decision_reject_feedback, last_finish, bounce_msg
        )
        return _Outcome(
            self._technical_bounce(bounce_msg), completed_step=True
        )
