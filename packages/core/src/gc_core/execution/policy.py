"""Shared orchestration policy, extracted without changing its decisions.

Every environmental dependency is supplied explicitly by the runtime bindings.
No tool registry, network, filesystem or live client is imported here.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

from gc_core.message_policy.user_message_policy import TECHNICAL_TYPE_TECHNICAL
from gc_core.settings import settings_of
from gc_core.utils.backend_errors import is_previous_response_cache_miss

if TYPE_CHECKING:
    from gc_core.execution.arguments_error import (
        WrongFunctionArgumentsException,
    )
    from gc_core.execution.state import ExecutionState as State


class ExecutionPolicy:
    def __init__(self, bindings):
        self.bindings = bindings

    async def _acall_blocking_hook(self, fn, *args: Any, **kwargs: Any):
        """Позвать «тяжёлый» колбэк вызывающего, не занимая event loop.

        Ядро передаёт сюда КОРУТИННЫЕ имена хуков, и такой хук ждётся нативно —
        ни потока, ни заморозки loop. Ветка с потоком осталась для чужих
        вызывающих, которые передали обычную функцию: позвать её «нативно
        асинхронно» нельзя в принципе, а звать инлайн значит заморозить loop
        приложения на всё время суммаризации.

        Что ядро не сползёт обратно на синхронные имена, стережёт AST-тест
        ``test_loop_hooks_are_native``.
        """
        if fn is None:
            return None
        if self.bindings.inspect.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        return await self.bindings.asyncio.to_thread(
            lambda: fn(*args, **kwargs)
        )

    async def _maybe_persist(
        self,
        callback,
        messages: list,
        state,
        *,
        pairing: bool = False,
    ) -> None:
        """Вызвать persist callback, если не активирован discard-after-stop guard.

        ``pairing=True`` — technical closing A/B при user-stop пишем всегда
        (иначе orphan function_call в jsonl).

        Колбэк по контракту возвращает ``None`` либо awaitable: ядро передаёт сюда
        асинхронный (история треда может лежать в базе, и ждать её надо нативно),
        а внешние вызывающие и тесты — обычный синхронный.
        """
        if callback is None:
            return
        if self.bindings.is_persist_discarded(state, pairing=pairing):
            return
        token = False
        if pairing:
            token = True
            state.allow_pairing_persist = True
        try:
            await self.bindings.await_callback(callback(messages))
        except self.bindings.ExecutionFailure:
            raise
        except Exception as persist_exc:
            self.bindings._fallback_logger.warning(
                "Thread messages persist warning: %s",
                persist_exc,
                exc_info=True,
            )
        finally:
            if token:
                state.allow_pairing_persist = False

    def _function_call_output_message(self, call_id: Any, output: str) -> Dict:
        return {
            "id": self.bindings.generate_message_id_func("fco"),
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        }

    async def _commit_user_stop_pairing(
        self,
        state: State,
        outputs: List[Dict],
        persist_messages_callback: Optional[Callable[[List[Dict]], None]],
    ) -> None:
        """Записать A/B в state.messages + jsonl несмотря на discard_writes_after_stop."""
        if not outputs:
            return
        to_append = self.bindings.filter_messages_not_in_state(
            list(getattr(state, "messages", []) or []),
            list(outputs),
        )
        if to_append:
            state.messages.extend(to_append)
        await self.bindings._maybe_persist(
            persist_messages_callback,
            to_append or list(outputs),
            state,
            pairing=True,
        )

    def _without_tool_call_pairs(
        self, messages: List[Dict], tool_name: str
    ) -> List[Dict]:
        """Убрать пары function_call + function_call_output для ``tool_name`` из preserved batch."""
        out: self.bindings.List[self.bindings.Dict] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if (
                i + 1 < len(messages)
                and isinstance(msg, dict)
                and msg.get("type") == "function_call"
                and str(msg.get("name") or "") == tool_name
            ):
                i += 2
                continue
            out.append(msg)
            i += 1
        return out

    def _make_technical_feedback_user_message(
        self,
        content: str,
        *,
        technical_type: str = TECHNICAL_TYPE_TECHNICAL,
        call_id: str | None = None,
    ) -> Dict[str, str]:
        msg = {
            "role": "user",
            "content": self.bindings.wrap_technical_user_content(
                content, technical_type=technical_type
            ),
            "id": self.bindings.generate_message_id_func("m"),
        }
        if call_id:
            msg["call_id"] = call_id
        return msg

    def _is_previous_response_cache_miss(self, exc: Exception) -> bool:
        return is_previous_response_cache_miss(exc)

    def _get_backend_api_mode(self, ctx: Any) -> str:
        raw_mode = str(settings_of(ctx).backend.api_mode).strip().lower()
        if raw_mode in {"responses", "chat_completions"}:
            return raw_mode
        raise ValueError(f"Unsupported backend_api_mode: {raw_mode!r}")

    def _mode_requires_backend_call(self, mode: str) -> bool:
        return mode in {"user_ask", "function_call_output"}

    def check_mode(self, messages, logger) -> Union[str, None]:
        if len(messages) == 0:
            raise ValueError("Empty messages list")
        if messages[-1].get("role", None) == "user":
            return "user_ask"
        for message in messages:
            if message.get("type", None) == "function_call":
                return "function_call"
        for message in messages:
            if message.get("type", None) == "function_call_output":
                return "function_call_output"
        if messages[-1].get("role", None) == "assistant":
            return "text_response"

        logger.debug("Got Undefined mode for messages:")
        logger.debug(str(messages))

        raise ValueError("Undefined mode")

    def _batch_has_pending_function_calls(self, messages: List[Dict]) -> bool:
        """True, если в батче есть function_call — их надо исполнить до step/token/tool лимитов.

        Иначе лимит между «модель уже вернула tools» и process_tool_calls даёт
        orphan call без output и теряет заявленную работу (sidecar/диск/summary).
        """
        return any(
            message.get("type") == "function_call"
            for message in messages or []
        )

    def group_messages(self, messages: List[Dict]) -> Dict[str, List[Dict]]:
        tool_calls = []
        text_messages = []
        tool_outputs = []
        for m in messages:
            m_type = m.get("type", "message")
            if m_type == "function_call":
                tool_calls.append(m)
            elif m_type == "message":
                text_messages.append(m)
            elif m_type == "function_call_output":
                tool_outputs.append(m)
            elif m_type == "reasoning":
                # Responses metadata stays in the original persisted batch;
                # it is not a tool or a user-visible assistant message.
                continue
            else:
                raise ValueError(f"message type error with: {m}")

        return {
            "tool_calls": tool_calls,
            "text_messages": text_messages,
            "tool_outputs": tool_outputs,
        }

    async def _aask_backend_and_record(self, messages, state: State, ctx: Any):
        """Сходить в бэкенд каноническим путём и запомнить заголовки ответа.

        Общее тело `aprocess_user_ask` и `aprocess_tool_outputs`: точки вызова у них
        разные (вопрос пользователя и выдача тулов), и тесты подменяют каждую по
        имени, поэтому обе остаются, а повтор тела ушёл сюда.
        """
        (
            resp_messages,
            response_id,
            res_headers,
        ) = await self.bindings._arequest_backend_canonical(
            messages,
            state,
            ctx,
        )
        state.set_resp_headers_metadata(res_headers or {})
        return resp_messages, response_id

    async def aprocess_user_ask(self, messages, state: State, ctx: Any):
        """Ход по сообщению пользователя."""
        return await self.bindings._aask_backend_and_record(
            messages, state, ctx
        )

    def partition_work_then_finish(
        self,
        tool_calls: List[Dict],
    ) -> tuple[List[Dict], List[Dict]]:
        """Work в порядке батча, затем не больше одного ``turn_finish``.

        Work — все вызовы кроме ``turn_finish``. Позиция finish work не режет.
        Первый finish идёт после work, остальные — skip. Успешный
        ``turn_ask_human`` подавляет весь оставшийся work и finish уже по
        фактическому output, не здесь.
        """
        work: self.bindings.List[self.bindings.Dict] = []
        finish_calls: self.bindings.List[self.bindings.Dict] = []
        for call in tool_calls:
            if str(call.get("name") or "") == self.bindings.FINISH_TOOL_NAME:
                finish_calls.append(call)
            else:
                work.append(call)
        if not finish_calls:
            return work, []
        return work + [finish_calls[0]], finish_calls[1:]

    def _first_finish_call(self, tool_calls: List[Dict]) -> Optional[Dict]:
        for call in tool_calls:
            if str(call.get("name") or "") == self.bindings.FINISH_TOOL_NAME:
                return call
        return None

    def _output_for_call(self, messages: List[Dict], call_id: Any) -> Any:
        for msg in messages:
            if (
                msg.get("type") == "function_call_output"
                and msg.get("call_id") == call_id
            ):
                return msg.get("output")
        return None

    def _closing_finish_after_batch(
        self,
        tool_calls: List[Dict],
        batch_outputs: List[Dict],
        *,
        stop_requested: bool,
    ) -> Optional[Dict]:
        """Закрывать ход или нет — только после батча, по фактическому output.

        Первый ``turn_finish`` исходного батча. ``ok: false`` (отказ тула / skip
        после ask) или user-stop — ход не сдаём. Иначе — этот вызов идёт в gate.
        """
        if stop_requested:
            return None
        finish = self.bindings._first_finish_call(tool_calls)
        if finish is None:
            return None
        finish_ok, _ = self.bindings.parse_tool_result_status(
            self.bindings._output_for_call(
                batch_outputs, finish.get("call_id")
            )
        )
        if finish_ok is False:
            return None
        return finish

    def _split_batch_new_around_finish(
        self,
        batch_new: List[Dict],
        finish_call_id: Any,
    ) -> tuple[List[Dict], List[Dict], List[Dict]]:
        """Префикс (work), finish-вывод, хвост (notice/compact) — без перестановки.

        Разрез по позиции первого ``function_call_output`` с ``finish_call_id``.
        Technical user (loop-detector, session_compact resume) остаются после
        finish, иначе в трейсе получается tool → user → tool и strict-бэкенд
        отвечает 400. Если finish-вывода нет — весь батч считается префиксом.
        """
        finish_idx = None
        for i, msg in enumerate(batch_new):
            if (
                msg.get("type") == "function_call_output"
                and msg.get("call_id") == finish_call_id
            ):
                finish_idx = i
                break
        if finish_idx is None:
            return list(batch_new), [], []
        return (
            batch_new[:finish_idx],
            batch_new[finish_idx : finish_idx + 1],
            batch_new[finish_idx + 1 :],
        )

    def _attach_truncation_hint(
        self, exc: WrongFunctionArgumentsException, state: Any
    ) -> None:
        """Дописать объяснение про усечение к фидбэку, не затирая подсказку самого
        тула (wrong_arguments_feedback_hint), которую положило место возбуждения."""
        if (
            self.bindings.classify_finish_kind(
                getattr(state, "last_finish_reason", None)
            )
            != self.bindings.FINISH_KIND_LENGTH
        ):
            return
        hints = [
            h
            for h in (
                getattr(exc, "extra_hint", None),
                self.bindings._TRUNCATED_ARGUMENTS_HINT,
            )
            if h
        ]
        exc.extra_hint = "\n\n".join(hints)

    def _extra_finish_skip_outputs(self, skipped: List[Dict]) -> List[Dict]:
        # skip extra-finish готовим заранее, чтобы сфлашить их до ask_human вместе
        # с уже выполненными тулами.
        return [
            {
                "id": self.bindings.generate_message_id_func("fco"),
                "type": "function_call_output",
                "call_id": m["call_id"],
                "output": (
                    f'{{"ok": false, "skipped": true, "message": '
                    f'"Skipped: extra {self.bindings.FINISH_TOOL_NAME} in this batch; '
                    f'one {self.bindings.FINISH_TOOL_NAME} per batch."}}'
                ),
            }
            for m in skipped
        ]

    def _after_ask_human_skip_output(self, call_id: Any) -> Dict[str, Any]:
        return {
            "id": self.bindings.generate_message_id_func("fco"),
            "type": "function_call_output",
            "call_id": call_id,
            "output": (
                f'{{"ok": false, "skipped": true, "message": '
                f'"Skipped: {self.bindings.ASK_HUMAN_TOOL_NAME} already received the '
                f"user reply. This call was generated before that reply; "
                f'reconsider it based on the reply in a new batch."}}'
            ),
        }

    def _not_started_outputs(
        self, remaining: List[Dict], state: State, ctx: Any
    ) -> List[Dict]:
        """Envelope B для вызовов, не стартовавших из-за user-stop."""
        outputs: self.bindings.List[self.bindings.Dict] = []
        for rem in remaining:
            rem_name = str(rem.get("name") or "")
            rem_out = self.bindings.not_started_due_to_user_stop_output(
                rem_name
            )
            rem_out = self.bindings.maybe_store_and_wrap_tool_output(
                rem_out,
                call_id=rem["call_id"],
                tool_name=rem_name,
                state=state,
                config=getattr(ctx, "config_dict", {}) or {},
                logger=getattr(ctx, "logger", None),
            )
            outputs.append(
                self.bindings._function_call_output_message(
                    rem["call_id"], rem_out
                )
            )
        return outputs

    async def _aexecute_tool_call(
        self,
        m: Dict,
        tool_name: str,
        tool_arguments: str,
        state: State,
        ctx: Any,
        run_agent_func,
    ) -> Tuple[Any, bool, Any]:
        """Один вызов тула с live-прогрессом и sidecar-трейсом → (сырой output, невалидные ли аргументы, ok)."""
        tool_call_id = m.get("call_id")
        call_id_str = str(tool_call_id) if tool_call_id is not None else None
        # intermediate_reply в UI — просто текст «· …», не строка тула ▸/✓,
        # поэтому ниже шлём отдельный сигнал, без «старт тула».
        is_intermediate = (
            tool_name == self.bindings.INTERMEDIATE_REPLY_TOOL_NAME
        )
        operations = getattr(ctx, "operations", None)
        clock = (
            operations.monotonic
            if operations is not None
            else self.bindings.time.monotonic
        )
        started_at = clock()

        # Live-прогресс субагента: «тул пошёл». На обычном parent-прогоне — no-op.
        # Sidecar persist параллельно (нужен thread identity; sink не требуется).
        # ask_human: только banner (ask_human/ask_human_done); nested tool-row
        # без sidecar → Ctrl+click молча no-op — не монтируем.
        persist_tool_trace = (
            not is_intermediate
            and tool_name != self.bindings.ASK_HUMAN_TOOL_NAME
        )
        emit_tool_row = persist_tool_trace
        if emit_tool_row:
            _todo_items = (
                self.bindings.todo_items_from_arguments(tool_arguments)
                if tool_name == self.bindings.SESSION_TODO_TOOL_NAME
                else None
            )
            self.bindings.emit_progress(
                ctx,
                state,
                "tool_start",
                tool_name=tool_name,
                tool_call_id=call_id_str,
                args_summary=self.bindings.brief_args_summary(
                    tool_arguments, tool_name=tool_name
                ),
                todo_items=_todo_items,
            )
        if persist_tool_trace:
            await self.bindings.apersist_subagent_trace(
                ctx, kind="call", payload=dict(m)
            )
        was_invalid = False
        try:
            # call_id для субагента: чтобы вложенные строки в TUI знали,
            # под какой корневой subagent_* они относятся.
            state.tool_calls += 1
            tool_res_raw, was_invalid = await self._ainvoke_tool(
                tool_name,
                tool_arguments,
                state,
                ctx,
                run_agent_func,
                tool_call_id,
            )
        except self.bindings.WrongFunctionArgumentsException as exc:
            # Отказ одного вызова — не отказ хода: невалидные аргументы становятся
            # output этого вызова (как unknown_tool), остальные вызовы батча
            # исполняются. Дальше общий путь: tool_end, sidecar, function_call_output.
            if getattr(exc, "call_id", None) is None:
                exc.call_id = tool_call_id
            self.bindings._attach_truncation_hint(exc, state)
            was_invalid = True
            tool_res_raw = exc.to_tool_output()
        # Live-прогресс: тул закончил — или текст intermediate_reply.
        # В историю треда это не пишется.
        elapsed_ms = int((clock() - started_at) * 1000)
        ok, error_brief = self.bindings.parse_tool_result_status(tool_res_raw)
        if is_intermediate:
            await self.bindings._aemit_intermediate_reply(
                ctx,
                state,
                tool_name,
                tool_arguments,
                tool_res_raw,
                call_id_str=call_id_str,
                elapsed_ms=elapsed_ms,
                ok=ok,
                error_brief=error_brief,
            )
            return tool_res_raw, was_invalid, ok
        if emit_tool_row:
            self.bindings.emit_progress(
                ctx,
                state,
                "tool_end",
                elapsed_ms=elapsed_ms,
                tool_name=tool_name,
                tool_call_id=call_id_str,
                ok=ok if ok is not None else True,
                error_brief=error_brief,
            )
        if persist_tool_trace:
            out_text = (
                tool_res_raw
                if isinstance(tool_res_raw, str)
                else self.bindings.json.dumps(tool_res_raw, ensure_ascii=False)
            )
            await self.bindings.apersist_subagent_trace(
                ctx,
                kind="error" if ok is False else "result",
                payload={
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": out_text,
                },
            )
        return tool_res_raw, was_invalid, ok

    async def _ainvoke_tool(
        self,
        tool_name,
        tool_arguments,
        state,
        ctx,
        run_agent_func,
        tool_call_id,
    ):
        operations = getattr(ctx, "operations", None)
        output = await self.bindings.aapply_tool(
            tool_name=tool_name,
            arguments=tool_arguments,
            ctx=ctx,
            state=state,
            run_agent_func=run_agent_func,
            call_id=tool_call_id,
            **(
                {"tool_operation_id": operations.next_operation_id("tool")}
                if operations is not None
                else {}
            ),
        )
        return output, False

    async def _aemit_intermediate_reply(
        self,
        ctx,
        state,
        tool_name,
        tool_arguments,
        tool_res_raw,
        *,
        call_id_str,
        elapsed_ms,
        ok,
        error_brief,
    ) -> None:
        prose = self.bindings.extract_intermediate_prose(
            tool_arguments, tool_res_raw
        )
        self.bindings.emit_progress(
            ctx,
            state,
            "intermediate_reply",
            elapsed_ms=elapsed_ms,
            tool_name=tool_name,
            tool_call_id=call_id_str,
            prose=prose,
            ok=ok if ok is not None else True,
            error_brief=error_brief,
        )
        ir_payload: self.bindings.Dict[str, self.bindings.Any] = {
            "prose": prose or ""
        }
        if call_id_str:
            ir_payload["call_id"] = call_id_str
        await self.bindings.apersist_subagent_trace(
            ctx, kind="intermediate_reply", payload=ir_payload
        )

    def _track_repeat_errors(
        self, state: State, tool_name: str, tool_res_raw: Any
    ) -> bool:
        """Счётчики повторов unknown-tool / internal-error на state; → был ли unknown-tool."""
        is_unknown = self.bindings.is_unknown_tool_output(tool_res_raw)
        if is_unknown:
            if tool_name == getattr(state, "last_unknown_tool_name", None):
                state.unknown_tool_repeat_count = (
                    int(getattr(state, "unknown_tool_repeat_count", 0) or 0)
                    + 1
                )
            else:
                state.last_unknown_tool_name = tool_name
                state.unknown_tool_repeat_count = 1
        else:
            state.last_unknown_tool_name = None
            state.unknown_tool_repeat_count = 0

        if self.bindings.is_internal_tool_error_output(tool_res_raw):
            if tool_name == getattr(
                state, "last_internal_error_tool_name", None
            ):
                state.internal_error_repeat_count = (
                    int(getattr(state, "internal_error_repeat_count", 0) or 0)
                    + 1
                )
            else:
                state.last_internal_error_tool_name = tool_name
                state.internal_error_repeat_count = 1
        else:
            state.last_internal_error_tool_name = None
            state.internal_error_repeat_count = 0
        return is_unknown

    def _apply_session_compact_to_batch(
        self,
        state: State,
        batch_snapshot: List[Dict],
        tools_outputs: List[Dict],
    ) -> List[Dict]:
        # Если внутри batch сработал session_compact — вместо сырых output'ов
        # вернуть сохранённые пары call/output + technical-сообщение «продолжай»,
        # чтобы не оставить висячий function_call и не тащить старое окно дальше.
        state.session_compact_applied = False
        snapshot = (
            getattr(state, "_session_compact_batch_snapshot", None)
            or batch_snapshot
        )
        if hasattr(state, "_session_compact_batch_snapshot"):
            delattr(state, "_session_compact_batch_snapshot")
        preserved = self.bindings.build_preserved_batch(
            current_messages=[*snapshot, *tools_outputs],
            mode="function_call_output",
            state_messages=list(getattr(state, "messages", []) or []),
        )
        preserved = self.bindings._without_tool_call_pairs(
            preserved, self.bindings.SESSION_COMPACT_TOOL_NAME
        )
        resume = self.bindings._make_technical_feedback_user_message(
            self.bindings._SESSION_COMPACT_RESUME_USER_TEXT,
            technical_type=self.bindings.TECHNICAL_TYPE_TECHNICAL,
        )
        return [*preserved, resume]

    async def _afinalize_tool_batch(
        self,
        state: State,
        ctx: Any,
        tools_outputs: List[Dict],
        batch_snapshot: List[Dict],
        *,
        aborted_by_user_stop: bool,
        persist_messages_callback: Optional[Callable[[List[Dict]], None]],
    ) -> List[Dict]:
        """Хвост батча: user-stop пары, session_compact, notice loop-детектора, gc."""
        if aborted_by_user_stop:
            # run_agent после stop часто выходит на check_stop до обычного persist —
            # закрывающие A/B пишем сразу (исключение discard_writes).
            await self.bindings._commit_user_stop_pairing(
                state, tools_outputs, persist_messages_callback
            )
            # Прогон остановлен: notice агенту не нужен, окно прошлого батча
            # не должно всплыть при продолжении сессии.
            self.bindings.clear_loop_detector(state)

        if getattr(state, "session_compact_applied", False):
            tools_outputs = self.bindings._apply_session_compact_to_batch(
                state, batch_snapshot, tools_outputs
            )

        notice = self.bindings.flush_loop_detector(state)
        if notice:
            tools_outputs.append(
                self.bindings._make_technical_feedback_user_message(
                    notice,
                    technical_type=self.bindings.TECHNICAL_TYPE_TECHNICAL,
                )
            )

        # C3: один collect на границе «батч тулов → LLM-шаг» — подбираем циклический
        # мусор между шагами. Default on для CLI/TUI, off для server; явный
        # gc.force_collect в конфиге перекрывает default.
        if self.bindings.gc_force_collect_enabled(
            getattr(ctx, "config_dict", {}) or {},
            is_tui=bool(getattr(ctx, "is_tui", False)),
            is_cli_mode=bool(getattr(ctx, "is_cli_mode", False)),
        ):
            # Рантайм может заменить сборку (Temporal: общий worker не
            # останавливается ради одного workflow); локально — gc.collect.
            collect = getattr(ctx, "collect_garbage", None)
            (collect or self.bindings.gc.collect)()

        return tools_outputs

    async def _abefore_tool_call(
        self,
        i: int,
        tool_name: str,
        state: State,
        *,
        prior: List[Dict],
        persist_messages_callback,
    ) -> None:
        # smart-гейт (спека §3): считаем КАЖДЫЙ исполняемый вызов, кроме завершителя хода.
        if tool_name not in self.bindings.TURN_FINALIZING_TOOLS:
            state.turn_tool_calls += 1
        # Human-wait (ask_human | confirm-in-tool): до блокировки сфлашить уже
        # посчитанные output'ы предыдущих тулов + skip'ы, чтобы TUI не держал их в running.
        if prior and (i > 0 or tool_name == self.bindings.ASK_HUMAN_TOOL_NAME):
            await self.bindings._maybe_persist(
                persist_messages_callback, prior, state
            )

    async def _afinish_tool_call(
        self,
        m: Dict,
        tool_name: str,
        tool_arguments: str,
        tool_res_raw: Any,
        state: State,
        ctx: Any,
    ) -> Dict:
        """Сжать output, обновить loop-детектор и intent-scope → function_call_output."""
        # Длинный output ужимаем в stored-envelope; остальное оставляем как есть.
        tool_res = self.bindings.maybe_store_and_wrap_tool_output(
            tool_res_raw,
            call_id=m["call_id"],
            tool_name=tool_name,
            state=state,
            config=getattr(ctx, "config_dict", {}) or {},
            logger=getattr(ctx, "logger", None),
        )
        if tool_name != self.bindings.FINISH_TOOL_NAME:
            self.bindings.observe_loop_detector(
                state, ctx, tool_name, tool_arguments
            )
            await self.bindings._acall_blocking_hook(
                self.bindings.amaybe_update_intent_scope_from_tool,
                tool_name=tool_name,
                tool_output_raw=tool_res_raw,
                tool_arguments=tool_arguments,
                state=state,
                ctx=ctx,
            )
        return self.bindings._function_call_output_message(
            m["call_id"], tool_res
        )

    async def aprocess_tool_calls(
        self,
        messages: List[Dict],
        state: State,
        ctx: Any,
        run_agent_func,
        persist_messages_callback: Optional[
            Callable[[List[Dict]], None]
        ] = None,
    ):
        """Исполнить batch function_call из ответа модели → список function_call_output.

        На каждый call по порядку:
          1) если сейчас пойдёт блокирующий ask_human — сначала записать уже готовые
             output'ы предыдущих тулов (и skip), чтобы в UI они не висели «running»;
          2) для субагента (вложенный прогон): сигнал «тул стартовал» в live-канал TUI;
          3) вызвать тул (для subagent_* ещё передаём call_id, чтобы nested-строки
             в TUI цеплялись к этой корневой строке субагента);
          4) для субагента: сигнал «тул закончил» или текст intermediate_reply;
             эти сигналы только в памяти TUI, в thread jsonl не пишутся;
          5) при необходимости ужать длинный output, обновить intent-scope,
             собрать function_call_output.

        До цикла по batch:
          - work (все кроме turn_finish) в порядке батча;
          - только первый turn_finish в executed; остальные extra-finish — skip
            сразу (сообщение «лишний finish», не «ход уже сдан»);
          - успешный ask в цикле даёт skip-output всему оставшемуся work и первому
            finish: эти вызовы были сгенерированы до ответа человека;
          - skip extra-finish готовим заранее, чтобы сфлашить их до ask_human
            вместе с уже выполненными тулами.

        User-stop: если ``state.stop_requested`` до/между тулами — remaining
        не ``apply_tool``, сразу envelope B; active (уже вернул A) + B сразу
        пишем в state/jsonl (исключение из discard_writes_after_stop).

        Live-прогресс субагента шлётся только если это вложенный ctx (есть
        parent_call_id и progress sink). У родителя sink может быть для чтения
        очереди, но сам parent тулы туда не пишет.
        """
        grouped_messages = self.bindings.group_messages(messages)
        if len(grouped_messages["tool_outputs"]) > 0:
            raise ValueError("Messages has tool_outputs")

        # Снимок batch до исполнения: если сработает session_compact, из него
        # соберём «что оставить» вместо сырого списка output'ов.
        batch_snapshot = list(messages)
        state._session_compact_batch_snapshot = batch_snapshot
        state.session_compact_applied = False

        tools_outputs = []
        # Work, затем не больше одного finish; skipped — лишние turn_finish.
        unknown_in_batch = 0
        invalid_in_batch = 0
        tool_calls = grouped_messages["tool_calls"]
        executed, skipped = self.bindings.partition_work_then_finish(
            tool_calls
        )
        # Skip-output'ы синтетические — считаем заранее, чтобы сфлашить ВМЕСТЕ с
        # executed-output'ами до блокирующего turn_ask_human (иначе пропущенный тул
        # тоже висит "running" до ответа юзера).
        skip_outputs = self.bindings._extra_finish_skip_outputs(skipped)
        aborted_by_user_stop = False
        successful_ask_human = False
        # Human-wait (ask_human | confirm-in-tool): до блокировки сфлашить уже
        # посчитанные output'ы предыдущих тулов + skip'ы (I2), чтобы TUI не держал
        # их в running на время wait. Полный батч персистится ещё раз на выходе
        # process_tool_calls (run_agent → _maybe_persist); уже записанные id
        # пропускаются в append_new_thread_messages по make_message_key.
        for i, m in enumerate(executed):
            # Между тулами / до старта — если stop, remaining не apply_tool → envelope B.
            if getattr(state, "stop_requested", False):
                tools_outputs.extend(
                    self.bindings._not_started_outputs(
                        executed[i:], state, ctx
                    )
                )
                aborted_by_user_stop = True
                break

            tool_name = str(m.get("name") or "")
            if successful_ask_human:
                tools_outputs.append(
                    self.bindings._after_ask_human_skip_output(m["call_id"])
                )
                continue
            # smart-гейт: считаем КАЖДЫЙ исполняемый вызов, кроме завершителя хода.
            # До try — упавший вызов (неверные аргументы) тоже считается.
            await self.bindings._abefore_tool_call(
                i,
                tool_name,
                state,
                prior=list(tools_outputs) + skip_outputs,
                persist_messages_callback=persist_messages_callback,
            )
            tool_arguments = str(m.get("arguments", ""))
            (
                tool_res_raw,
                was_invalid,
                ok,
            ) = await self.bindings._aexecute_tool_call(
                m, tool_name, tool_arguments, state, ctx, run_agent_func
            )
            invalid_in_batch += int(was_invalid)
            if tool_name == self.bindings.ASK_HUMAN_TOOL_NAME and ok is True:
                successful_ask_human = True
            unknown_in_batch += int(
                self.bindings._track_repeat_errors(
                    state, tool_name, tool_res_raw
                )
            )

            # Длинный output ужимаем в stored-envelope; остальное оставляем как есть.
            tools_outputs.append(
                await self.bindings._afinish_tool_call(
                    m, tool_name, tool_arguments, tool_res_raw, state, ctx
                )
            )

            # После apply: если стопнули mid-tool (envelope A от runner) — хвост batch → B.
            if getattr(state, "stop_requested", False):
                tools_outputs.extend(
                    self.bindings._not_started_outputs(
                        executed[i + 1 :], state, ctx
                    )
                )
                aborted_by_user_stop = True
                break

        # Skip-output'ы уже посчитаны выше и сфлашены перед блокировкой — добавляем в
        # возврат (тред/state дедупят повторный персист по make_message_key).
        state.last_batch_unknown_tool_count = unknown_in_batch
        state.last_batch_invalid_arguments_count = invalid_in_batch
        tools_outputs.extend(skip_outputs)

        return await self.bindings._afinalize_tool_batch(
            state,
            ctx,
            tools_outputs,
            batch_snapshot,
            aborted_by_user_stop=aborted_by_user_stop,
            persist_messages_callback=persist_messages_callback,
        )

    async def aprocess_tool_outputs(self, messages, state: State, ctx: Any):
        """Ход по выдаче тулов."""
        return await self.bindings._aask_backend_and_record(
            messages, state, ctx
        )

    def _pickup_pending_as_new_turn(
        self,
        get_pending_messages: Optional[Callable[[], List[Dict]]],
        state: State,
        ctx: Any,
    ) -> Optional[List[Dict]]:
        """Ход закончен (finish-gate accept / text-response accept, спека §6.4): очередь
        непуста → фиксируем модель нового хода (``begin_turn``) и возвращаем сообщения,
        которыми продолжится цикл вместо возврата из ``run_agent``. ``None`` — очередь
        пуста, ход завершается как обычно."""
        if get_pending_messages is None:
            return None
        pending = get_pending_messages()
        if not pending:
            return None
        # Новый пользовательский ход: окно прошлого хода не должно давать
        # ложный повтор на первом же чтении с теми же аргументами.
        self.bindings.clear_loop_detector(state)
        try:
            self.bindings.begin_turn_main_trace(state, ctx)
        except Exception as exc:
            # Очередь УЖЕ слита (`get_pending_messages` — drain, не peek): уронив ход
            # здесь, мы потеряли бы набранное пользователем сообщение бесследно —
            # вернуть его в очередь некуда. Поэтому ход продолжаем на модели прошлого
            # хода: тот же честный отказ придёт из `_request_backend_canonical` (единая
            # точка текста), но сообщение к тому моменту уже в трейсе и в логе.
            logger = (
                getattr(ctx, "logger", None) or self.bindings._fallback_logger
            )
            logger.warning(
                "фиксация модели нового хода не удалась (%s) — подхваченное сообщение "
                "не теряем, ход продолжается на прежней модели",
                exc,
            )
        return pending
