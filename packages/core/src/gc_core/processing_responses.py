import functools
import asyncio
import inspect
import gc
import json
import time
from types import SimpleNamespace

from gc_core.config import parse_chat_completions_max_tokens, resolve_tui_stream_enabled
from gc_core.config_modules.gc import gc_force_collect_enabled
from gc_core.intent_guard.feedback import build_finish_gate_feedback, build_finish_gate_reject_technical_user_content
from gc_core.constants.tool_names import ASK_HUMAN_TOOL_NAME, FINISH_TOOL_NAME, INTERMEDIATE_REPLY_TOOL_NAME, SESSION_COMPACT_TOOL_NAME, SESSION_TODO_TOOL_NAME, TURN_FINALIZING_TOOLS
from gc_core.run_summary_coordinator import build_preserved_batch, filter_messages_not_in_state
from gc_core.intent_guard.guard import FinishGateGuard
from gc_core.intent_guard.text_answer_judge import ashould_return_text_answer
from gc_core.intent_guard.turn_finish_emit import apply_finish_gate_to_turn_finish_output_in_state, get_function_call_output_text
from gc_core.intent_scope.hook import maybe_update_intent_scope_from_tool
from gc_core.intent_scope.final_answer import maybe_record_final_answer_intent
from gc_core.message_policy.cycle_stop import aappend_cycle_stop_tool_outputs_for_batch
from gc_core.message_policy.stop_persist import is_persist_discarded
from gc_core.message_policy.user_stop import not_started_due_to_user_stop_output
from gc_core.summarize.limit_trace_summary import build_limit_envelope
from gc_core.request_backend_policy import arequest_chat_completions_with_policy
from gc_core.tools import aapply_tool, WrongFunctionArgumentsException, is_unknown_tool_output
from gc_core.tools.session.stored_outputs.wrapping import maybe_store_and_wrap_tool_output
from gc_core.tools.subagents.progress import brief_args_summary, emit_progress, extract_intermediate_prose, parse_tool_result_status, todo_items_from_arguments
from gc_core.tools.subagents.sidecar import apersist_subagent_trace
from gc_core.utils.connect_backend import create_call_headers
from gc_core.utils.connect_backend import arequest_backend_responses as arequest_backend
from gc_core.utils.ssl_config import get_backend_client_cert
from gc_core.utils.logging_module import human_console_enabled
from gc_core.utils.aio_compat import await_callback
from gc_core.utils.backend_errors import is_auth_error, is_context_overflow_error

# Переполнение контекста → force-компакция + повтор запроса; лимит на случай,
# когда даже после сжатия история всё ещё не влезает (единичное огромное сообщение).
_MAX_CONTEXT_OVERFLOW_RETRIES = 3
from gc_core.utils.connect_backend import create_call_data_responses as create_call_data
from gc_core.utils.connect_backend import create_call_data_completions
from gc_core.utils.convertations import output_chat_completion_to_response, sanitize_messages_for_chat_completions
from gc_core.utils.format_messages import format_message_responses
from gc_core.utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func

from gc_core.states import State
from gc_core.usage.account import CATEGORY_MAIN as USAGE_CATEGORY_MAIN
from gc_core.usage.account import UsageAccount
from gc_core.usage.context import usage_scope
from typing import List, Dict, Union, Tuple, Any, Callable, Optional
from gc_core.message_policy.no_tool_call_bounce import NO_TOOL_CALL_BOUNCE_MESSAGE, extract_last_assistant_text, format_no_tool_call_bounce
from gc_core.message_policy.tool_usage_notice.apply import apply_tool_usage_notice_before_llm
from gc_core.message_policy.user_message_policy import TECHNICAL_TYPE_TECHNICAL, TECHNICAL_TYPE_ERROR_MESSAGE, wrap_technical_user_content, build_invalid_tool_call_feedback_message

import logging
from gc_core.utils.portal import get_portal

_fallback_logger = logging.getLogger(__name__)






async def _acall_blocking_hook(fn, *args: Any, **kwargs: Any):
    """Позвать «тяжёлый» колбэк вызывающего, не занимая event loop.

    Компакция контекста и intent-scope делают собственные LLM-вызовы и
    остаются синхронными (подсистема большая, а работает редко — раз в N
    шагов). Из асинхронного цикла их нельзя звать напрямую двумя способами
    сразу: прямой вызов заморозил бы loop на всё время суммаризации, а
    синхронный шим внутри них упёрся бы в портал и поднял
    SyncApiInsideEventLoopError.

    Поэтому — в поток: loop свободен, а LLM-вызовы внутри всё равно уходят на
    портальный loop асинхронно. Корутинный колбэк (если вызывающий уже
    переехал) просто ждём.
    """
    if fn is None:
        return None
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(lambda: fn(*args, **kwargs))


async def _maybe_persist(
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
    if is_persist_discarded(state, pairing=pairing):
        return
    token = False
    if pairing:
        token = True
        state.allow_pairing_persist = True
    try:
        await await_callback(callback(messages))
    except Exception as persist_exc:
        _fallback_logger.warning(
            "Thread messages persist warning: %s",
            persist_exc,
            exc_info=True,
        )
    finally:
        if token:
            state.allow_pairing_persist = False


def _function_call_output_message(call_id: Any, output: str) -> Dict:
    return {
        "id": generate_message_id_func("fco"),
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }


async def _commit_user_stop_pairing(
    state: State,
    outputs: List[Dict],
    persist_messages_callback: Optional[Callable[[List[Dict]], None]],
) -> None:
    """Записать A/B в state.messages + jsonl несмотря на discard_writes_after_stop."""
    if not outputs:
        return
    to_append = filter_messages_not_in_state(
        list(getattr(state, "messages", []) or []),
        list(outputs),
    )
    if to_append:
        state.messages.extend(to_append)
    await _maybe_persist(
        persist_messages_callback,
        to_append or list(outputs),
        state,
        pairing=True,
    )


def _summarize_call_data_for_log(call_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Короткая сводка запроса для корреляции с ответом backend.
    """
    messages = call_data.get("messages")
    tools = call_data.get("tools")
    return {
        "model": call_data.get("model"),
        "max_tokens": call_data.get("max_tokens"),
        "messages_count": len(messages) if isinstance(messages, list) else None,
        "tools_count": len(tools) if isinstance(tools, list) else None,
    }


_SESSION_COMPACT_RESUME_USER_TEXT = (
    "session_compact finished. The dialog summary user message in the trace is the new "
    "baseline (updated session memory, last_dialog_steps, user intent). Continue from "
    "that summary. The pre-compaction step-by-step trace is no longer in context."
)


def _without_tool_call_pairs(messages: List[Dict], tool_name: str) -> List[Dict]:
    """Убрать пары function_call + function_call_output для ``tool_name`` из preserved batch."""
    out: List[Dict] = []
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
    content: str, *, technical_type: str = TECHNICAL_TYPE_TECHNICAL,
    call_id: str | None = None,
) -> Dict[str, str]:
    msg = {
        "role": "user",
        "content": wrap_technical_user_content(
            content, technical_type=technical_type
        ),
        "id": generate_message_id_func("m"),
    }
    if call_id:
        msg["call_id"] = call_id
    return msg


def _is_previous_response_cache_miss(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "previous_response_id" in text
        and ("not found" in text or "cache" in text)
    ) or ("cache" in text and "not found" in text)


def _get_backend_api_mode(ctx: Any) -> str:
    cfg = getattr(ctx, "config_dict", {}) or {}
    raw_mode = str(cfg.get("backend_api_mode", "responses")).strip().lower()
    if raw_mode in {"responses", "chat_completions"}:
        return raw_mode
    raise ValueError(f"Unsupported backend_api_mode: {raw_mode!r}")


def _mode_requires_backend_call(mode: str) -> bool:
    return mode in {"user_ask", "function_call_output"}


async def _arequest_backend_canonical(
    messages: List[Dict],
    state: State,
    ctx: Any,
    *,
    set_previous_response_id: bool = True,
) -> Tuple[List[Dict], str, Dict]:
    """Обёртка над одиночной попыткой: на 401 (истёкший токен доступа) пере-выпускает
    токен (`auth_connector.get_token(force=True)`) и повторяет запрос РОВНО один раз.
    Повторный 401 после свежего токена — настоящий auth-fail, уходит наверх внятно.
    Прочие ошибки — наверх без изменений. Все call-site получают это единообразно."""
    auth_connector = getattr(ctx, "auth_connector", None)
    try:
        return await _arequest_backend_canonical_once(
            messages, state, ctx, set_previous_response_id=set_previous_response_id
        )
    except Exception as exc:
        if auth_connector is None or not is_auth_error(exc):
            raise
        run_logger = getattr(ctx, "logger", None) or _fallback_logger
        run_logger.warning(
            "backend auth 401 → пере-выпуск токена и повтор запроса один раз: %s", exc
        )
        auth_connector.get_token(force=True)
        return await _arequest_backend_canonical_once(
            messages, state, ctx, set_previous_response_id=set_previous_response_id
        )


async def _arequest_backend_canonical_once(
    messages: List[Dict],
    state: State,
    ctx: Any,
    *,
    set_previous_response_id: bool = True,
) -> Tuple[List[Dict], str, Dict]:
    """
    Унифицированный вызов backend для двух режимов API (одна попытка).

    Возвращает:
    - канонические сообщения (responses-like),
    - backend response id,
    - backend response headers (может быть пустым dict).
    """
    call_headers = create_call_headers(state, ctx)
    cfg = getattr(ctx, "config_dict", {}) or {}
    verify = cfg.get("use_ssl", True)
    cert = get_backend_client_cert(cfg)
    use_retry = cfg.get("use_retry", False)
    request_timeout_sec = float(cfg["backend_request_timeout_sec"])
    backend_mode = _get_backend_api_mode(ctx)

    if backend_mode == "responses":
        url = cfg["agent_url"]
        call_data = create_call_data(
            messages,
            state,
            set_previous_response_id=set_previous_response_id,
        )
        with usage_scope(getattr(state, "usage", None), USAGE_CATEGORY_MAIN, backend_mode):
            res, res_headers = await arequest_backend(
                call_data,
                call_headers,
                url=url,
                verify=verify,
                cert=cert,
                use_retry=use_retry,
                logger=getattr(ctx, "logger", None),
                timeout=request_timeout_sec,
                config_proxy=cfg.get("proxy"),
            )
        resp_messages = res["output"]
        response_id = str(res["id"])
        return resp_messages, response_id, res_headers

    url = cfg["completions_url"]
    sanitized_messages = sanitize_messages_for_chat_completions(
        messages,
        invalid_tool_call_factory=build_invalid_tool_call_feedback_message,
    )
    sanitized_state_messages = sanitize_messages_for_chat_completions(
        getattr(state, "messages", []) or [],
        invalid_tool_call_factory=build_invalid_tool_call_feedback_message,
    )
    chat_state = SimpleNamespace(
        messages=sanitized_state_messages,
        tools=getattr(state, "tools", []),
        default_model=getattr(state, "default_model", None),
    )
    completions_max_tokens = parse_chat_completions_max_tokens(
        cfg.get("chat_completions_max_tokens", 8096), default=8096
    )
    min_response_tokens = parse_chat_completions_max_tokens(
        cfg.get("chat_completions_min_response_tokens", 1024),
        default=1024,
    )

    def _create_payload(max_tokens: int) -> Dict[str, Any]:
        return create_call_data_completions(
            sanitized_messages,
            chat_state,
            max_tokens=max_tokens,
        )

    _stream_on = resolve_tui_stream_enabled(cfg)
    _on_delta = getattr(ctx, "on_stream_delta", None) if _stream_on else None

    with usage_scope(getattr(state, "usage", None), USAGE_CATEGORY_MAIN, backend_mode):
        res, call_data = await arequest_chat_completions_with_policy(
            payload_factory=_create_payload,
            initial_max_tokens=completions_max_tokens,
            min_response_tokens=min_response_tokens,
            headers=call_headers,
            url=url,
            verify=verify,
            cert=cert,
            use_retry=use_retry,
            retry_embedded_choice_errors=cfg.get("retry_embedded_choice_errors", True),
            logger=getattr(ctx, "logger", None),
            on_delta=_on_delta,
            request_label="main_agent",
            runtime_notices_enabled=bool(getattr(ctx, "is_tui", False)),
            notice_url=url,
            timeout=request_timeout_sec,
            config_proxy=cfg.get("proxy"),
        )
    if not isinstance(res, dict):
        raise ValueError("chat/completions backend response must be an object")
    _run_log = getattr(ctx, "logger", None) or _fallback_logger
    _choices_raw = res.get("choices")
    _finish = None
    if isinstance(_choices_raw, list) and len(_choices_raw) > 0:
        _ch0 = _choices_raw[0]
        if isinstance(_ch0, dict):
            _finish = _ch0.get("finish_reason")
    # Сохраняем finish_reason на state — catch-блок WrongFunctionArgumentsException
    # использует его, чтобы отличить усечённый JSON (length) от обычной невалидности.
    state.last_finish_reason = _finish
    _run_log.info(
        "chat/completions response meta: max_tokens_requested=%s finish_reason=%s usage=%s",
        call_data.get("max_tokens"),
        _finish,
        res.get("usage"),
    )
    # Для режима chat/completions всегда используем только choices[0].
    first_choice_payload = dict(res)
    choices = res.get("choices")
    if isinstance(choices, list) and len(choices) > 0:
        first_choice_payload["choices"] = [choices[0]]
    canonical = output_chat_completion_to_response(
        first_choice_payload,
        logger_override=getattr(ctx, "logger", _fallback_logger),
        log_context={
            "url": url,
            "request_summary": _summarize_call_data_for_log(call_data),
        },
    )
    resp_messages = canonical["output"]
    response_id = str(canonical.get("id") or res.get("id") or "")
    if not response_id:
        raise ValueError("chat/completions response id is empty")
    return resp_messages, response_id, {}


def check_mode(messages, logger) -> Union[str, None]:
    if len(messages) == 0:
        raise ValueError('Empty messages list')
    if messages[-1].get('role', None) == 'user':
        return 'user_ask'
    for message in messages:
        if message.get('type', None) == 'function_call':
            return 'function_call'
    for message in messages:
        if message.get('type', None) == 'function_call_output':
            return 'function_call_output'
    if messages[-1].get('role', None) == 'assistant':
        return 'text_response'

    logger.debug('Got Undefined mode for messages:')
    logger.debug(str(messages))

    raise ValueError('Undefined mode')


def _batch_has_pending_function_calls(messages: List[Dict]) -> bool:
    """True, если в батче есть function_call — их надо исполнить до step/token/tool лимитов.

    Иначе лимит между «модель уже вернула tools» и process_tool_calls даёт
    orphan call без output и теряет заявленную работу (sidecar/диск/summary).
    """
    for message in messages or []:
        if message.get("type") == "function_call":
            return True
    return False


def group_messages(messages: List[Dict]) -> Dict[str, List[Dict]]:
    tool_calls = []
    text_messages = []
    tool_outputs = []
    for m in messages:
        m_type = m.get('type', 'message')
        if m_type == 'function_call':
            tool_calls.append(m)
        elif m_type == 'message':
            text_messages.append(m)
        elif m_type == 'function_call_output':
            tool_outputs.append(m)
        else:
            raise ValueError(f'message type error with: {m}')

    return {
        'tool_calls': tool_calls,
        'text_messages': text_messages,
        'tool_outputs': tool_outputs
    }


def check_trace_finished(messages: List[Dict]) -> Union[Tuple[str, str], None]:
    if len(messages) == 0:
        raise ValueError('Empty messages list')

    grouped_messages = group_messages(messages)
    for m in grouped_messages['tool_calls']:
        if m['name'] == FINISH_TOOL_NAME:
            return m['arguments'], m['call_id']

    return None


async def aprocess_user_ask(messages, state: State, ctx: Any):
    resp_messages, response_id, res_headers = await _arequest_backend_canonical(
        messages,
        state,
        ctx,
    )
    state.set_resp_headers_metadata(res_headers or {})
    return resp_messages, response_id


def split_at_first_finalizer(tool_calls: List[Dict]) -> tuple[List[Dict], List[Dict]]:
    """Разбивает tool_calls на (executed, skipped): всё до и включая первый
    turn-финализирующий тул исполняется; остальное в этом ответе — пропускается
    (ход уже завершён). intermediate_reply финализатором не считается."""
    for i, m in enumerate(tool_calls):
        if str(m.get("name") or "") in TURN_FINALIZING_TOOLS:
            return tool_calls[: i + 1], tool_calls[i + 1:]
    return tool_calls, []


async def aprocess_tool_calls(
    messages: List[Dict],
    state: State,
    ctx: Any,
    run_agent_func,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]] = None,
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
      - после первого turn-финализатора (finish / ask_human) остальные call'ы
        не исполняем — сразу synthetic skip-output;
      - skip готовим заранее, чтобы успеть записать их до ask_human вместе
        с уже выполненными тулами.

    User-stop: если ``state.stop_requested`` до/между тулами — remaining
    не ``apply_tool``, сразу envelope B; active (уже вернул A) + B сразу
    пишем в state/jsonl (исключение из discard_writes_after_stop).

    Live-прогресс субагента шлётся только если это вложенный ctx (есть
    parent_call_id и progress sink). У родителя sink может быть для чтения
    очереди, но сам parent тулы туда не пишет.
    """
    grouped_messages = group_messages(messages)
    if len(grouped_messages['tool_outputs']) > 0:
        raise ValueError('Messages has tool_outputs')

    # Снимок batch до исполнения: если сработает session_compact, из него
    # соберём «что оставить» вместо сырого списка output'ов.
    batch_snapshot = list(messages)
    state._session_compact_batch_snapshot = batch_snapshot
    state.session_compact_applied = False

    tools_outputs = []
    # Один turn-финализатор на ход: executed исполняем, skipped — только synthetic output.
    unknown_in_batch = 0
    executed, skipped = split_at_first_finalizer(grouped_messages["tool_calls"])
    # Skip-output'ы синтетические — считаем заранее, чтобы сфлашить ВМЕСТЕ с
    # executed-output'ами до блокирующего turn_ask_human (иначе пропущенный тул
    # тоже висит "running" до ответа юзера).
    skip_outputs = []
    if skipped:
        finalizer = str(executed[-1].get("name") or "the finalizing tool")
        for m in skipped:
            skip_outputs.append({
                "id": generate_message_id_func("fco"),
                "type": "function_call_output",
                "call_id": m["call_id"],
                "output": (
                    f'{{"ok": false, "skipped": true, "message": '
                    f'"Skipped: turn already finalized by {finalizer}; '
                    f'one finalizer per turn."}}'
                ),
            })
    aborted_by_user_stop = False
    # Human-wait (ask_human | confirm-in-tool): до блокировки сфлашить уже
    # посчитанные output'ы предыдущих тулов + skip'ы (I2), чтобы TUI не держал
    # их в running на время wait. Полный батч персистится ещё раз на выходе
    # process_tool_calls (run_agent → _maybe_persist); уже записанные id
    # пропускаются в append_new_thread_messages по make_message_key.
    for i, m in enumerate(executed):
        # Между тулами / до старта — если stop, remaining не apply_tool → envelope B.
        if getattr(state, "stop_requested", False):
            for rem in executed[i:]:
                rem_name = str(rem.get("name") or "")
                rem_out = not_started_due_to_user_stop_output(rem_name)
                rem_out = maybe_store_and_wrap_tool_output(
                    rem_out,
                    call_id=rem["call_id"],
                    tool_name=rem_name,
                    state=state,
                    config=getattr(ctx, "config_dict", {}) or {},
                    logger=getattr(ctx, "logger", None),
                )
                tools_outputs.append(
                    _function_call_output_message(rem["call_id"], rem_out)
                )
            aborted_by_user_stop = True
            break

        tool_name = str(m.get("name") or "")
        # smart-гейт (спека §3): считаем КАЖДЫЙ исполняемый вызов, кроме завершителя
        # хода. До try — упавший вызов (неверные аргументы) тоже считается. turn_finish
        # сюда не попадает (его путь — check_trace_finished мимо process_tool_calls).
        if tool_name not in TURN_FINALIZING_TOOLS:
            state.turn_tool_calls += 1
        prior = list(tools_outputs) + skip_outputs
        if prior and (i > 0 or tool_name == ASK_HUMAN_TOOL_NAME):
            await _maybe_persist(persist_messages_callback, prior, state)
        tool_arguments = str(m.get("arguments", ""))
        tool_call_id = m.get("call_id")
        call_id_str = str(tool_call_id) if tool_call_id is not None else None
        # intermediate_reply в UI — просто текст «· …», не строка тула ▸/✓,
        # поэтому ниже шлём отдельный сигнал, без «старт тула».
        is_intermediate = tool_name == INTERMEDIATE_REPLY_TOOL_NAME
        started_at = time.monotonic()

        # Live-прогресс субагента: «тул пошёл». На обычном parent-прогоне — no-op.
        # Sidecar persist параллельно (нужен thread identity; sink не требуется).
        # ask_human: только banner (ask_human/ask_human_done); nested tool-row
        # без sidecar → Ctrl+click молча no-op — не монтируем.
        persist_tool_trace = (
            not is_intermediate and tool_name != ASK_HUMAN_TOOL_NAME
        )
        emit_tool_row = persist_tool_trace
        if emit_tool_row:
            _todo_items = (
                todo_items_from_arguments(tool_arguments)
                if tool_name == SESSION_TODO_TOOL_NAME else None
            )
            emit_progress(
                ctx,
                state,
                "tool_start",
                tool_name=tool_name,
                tool_call_id=call_id_str,
                args_summary=brief_args_summary(tool_arguments, tool_name=tool_name),
                todo_items=_todo_items,
            )
        if persist_tool_trace:
            await apersist_subagent_trace(ctx, kind="call", payload=dict(m))
        try:
            # call_id для субагента: чтобы вложенные строки в TUI знали,
            # под какой корневой subagent_* они относятся.
            state.tool_calls += 1
            tool_res_raw = await aapply_tool(
                tool_name=tool_name,
                arguments=tool_arguments,
                ctx=ctx,
                state=state,
                run_agent_func=run_agent_func,
                call_id=tool_call_id,
            )
        except WrongFunctionArgumentsException as exc:
            if getattr(exc, "call_id", None) is None:
                exc.call_id = tool_call_id
            # Даже при ошибке аргументов закрыть «бегущую» строку в TUI.
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            if emit_tool_row:
                emit_progress(
                    ctx,
                    state,
                    "tool_end",
                    elapsed_ms=elapsed_ms,
                    tool_name=tool_name,
                    tool_call_id=call_id_str,
                    ok=False,
                    error_brief=str(exc),
                )
            if persist_tool_trace:
                await apersist_subagent_trace(
                    ctx,
                    kind="error",
                    payload={
                        "type": "function_call_output",
                        "call_id": tool_call_id,
                        "output": str(exc),
                    },
                )
            raise
        # Live-прогресс: тул закончил — или текст intermediate_reply.
        # В историю треда это не пишется.
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        ok, error_brief = parse_tool_result_status(tool_res_raw)
        if is_intermediate:
            prose = extract_intermediate_prose(tool_arguments, tool_res_raw)
            emit_progress(
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
            ir_payload: Dict[str, Any] = {"prose": prose or ""}
            if call_id_str:
                ir_payload["call_id"] = call_id_str
            await apersist_subagent_trace(
                ctx, kind="intermediate_reply", payload=ir_payload
            )
        else:
            if emit_tool_row:
                emit_progress(
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
                    else json.dumps(tool_res_raw, ensure_ascii=False)
                )
                await apersist_subagent_trace(
                    ctx,
                    kind="error" if ok is False else "result",
                    payload={
                        "type": "function_call_output",
                        "call_id": tool_call_id,
                        "output": out_text,
                    },
                )

        if is_unknown_tool_output(tool_res_raw):
            unknown_in_batch += 1
            if tool_name == getattr(state, "last_unknown_tool_name", None):
                state.unknown_tool_repeat_count = (
                    int(getattr(state, "unknown_tool_repeat_count", 0) or 0) + 1
                )
            else:
                state.last_unknown_tool_name = tool_name
                state.unknown_tool_repeat_count = 1
        else:
            state.last_unknown_tool_name = None
            state.unknown_tool_repeat_count = 0

        # Длинный output ужимаем в stored-envelope; остальное оставляем как есть.
        tool_res = maybe_store_and_wrap_tool_output(
            tool_res_raw,
            call_id=m["call_id"],
            tool_name=tool_name,
            state=state,
            config=getattr(ctx, "config_dict", {}) or {},
            logger=getattr(ctx, "logger", None),
        )
        if tool_name != FINISH_TOOL_NAME:
            await _acall_blocking_hook(maybe_update_intent_scope_from_tool,
                tool_name=tool_name,
                tool_output_raw=tool_res_raw,
                tool_arguments=tool_arguments,
                state=state,
                ctx=ctx,
            )
        new_message = _function_call_output_message(m["call_id"], tool_res)

        tools_outputs.append(new_message)

        # После apply: если стопнули mid-tool (envelope A от runner) — хвост batch → B.
        if getattr(state, "stop_requested", False):
            for rem in executed[i + 1 :]:
                rem_name = str(rem.get("name") or "")
                rem_out = not_started_due_to_user_stop_output(rem_name)
                rem_out = maybe_store_and_wrap_tool_output(
                    rem_out,
                    call_id=rem["call_id"],
                    tool_name=rem_name,
                    state=state,
                    config=getattr(ctx, "config_dict", {}) or {},
                    logger=getattr(ctx, "logger", None),
                )
                tools_outputs.append(
                    _function_call_output_message(rem["call_id"], rem_out)
                )
            aborted_by_user_stop = True
            break

    # Skip-output'ы уже посчитаны выше и сфлашены перед блокировкой — добавляем в
    # возврат (тред/state дедупят повторный персист по make_message_key).
    state.last_batch_unknown_tool_count = unknown_in_batch
    tools_outputs.extend(skip_outputs)

    if aborted_by_user_stop:
        # run_agent после stop часто выходит на check_stop до обычного persist —
        # закрывающие A/B пишем сразу (исключение discard_writes).
        await _commit_user_stop_pairing(state, tools_outputs, persist_messages_callback)

    # Если внутри batch сработал session_compact — вместо сырых output'ов
    # вернуть сохранённые пары call/output + technical-сообщение «продолжай»,
    # чтобы не оставить висячий function_call и не тащить старое окно дальше.
    if getattr(state, "session_compact_applied", False):
        state.session_compact_applied = False
        snapshot = getattr(state, "_session_compact_batch_snapshot", None) or batch_snapshot
        if hasattr(state, "_session_compact_batch_snapshot"):
            delattr(state, "_session_compact_batch_snapshot")
        preserved = build_preserved_batch(
            current_messages=[*snapshot, *tools_outputs],
            mode="function_call_output",
            state_messages=list(getattr(state, "messages", []) or []),
        )
        preserved = _without_tool_call_pairs(preserved, SESSION_COMPACT_TOOL_NAME)
        resume = _make_technical_feedback_user_message(
            _SESSION_COMPACT_RESUME_USER_TEXT,
            technical_type=TECHNICAL_TYPE_TECHNICAL,
        )
        tools_outputs = [*preserved, resume]

    # C3: один collect на границе «батч тулов → LLM-шаг» — подбираем циклический
    # мусор между шагами. Default on для CLI/TUI, off для server; явный
    # gc.force_collect в конфиге перекрывает default.
    if gc_force_collect_enabled(
        getattr(ctx, "config_dict", {}) or {},
        is_tui=bool(getattr(ctx, "is_tui", False)),
        is_cli_mode=bool(getattr(ctx, "is_cli_mode", False)),
    ):
        gc.collect()

    return tools_outputs


async def aprocess_tool_outputs(messages, state: State, ctx: Any):
    resp_messages, response_id, res_headers = await _arequest_backend_canonical(
        messages,
        state,
        ctx,
    )
    state.set_resp_headers_metadata(res_headers or {})
    return resp_messages, response_id


async def arun_agent(
    messages,
    state: State,
    ctx: Any,
    *,
    get_full_history_messages: Optional[Callable[[], List[Dict]]] = None,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]] = None,
    pre_backend_compaction_hook: Optional[Callable[[List[Dict], str], None]] = None,
    turn_end_compaction_hook: Optional[Callable[[], None]] = None,
    force_compact_hook: Optional[Callable[[], None]] = None,
    get_pending_messages: Optional[Callable[[], List[Dict]]] = None,
    check_stop: Optional[Callable[[], bool]] = None,
    session_meta: Optional[Dict[str, Any]] = None,
    mark_finalizing: Optional[Callable[[bool], None]] = None,
    mark_finish_gate_checking: Optional[Callable[[bool], None]] = None,
) -> Union[str, None]:
    tool_error_cnt = 0
    context_overflow_retries = 0
    run_logger = getattr(ctx, "logger", None) or _fallback_logger
    cfg = getattr(ctx, "config_dict", {}) or {}
    console_human = human_console_enabled(cfg)
    max_tool_errors = cfg.get("no_tool_call_max_count", 0)
    session_token_limit = cfg.get("session_token_limit")
    max_tool_calls_limit = cfg.get("max_tool_calls")
    max_unknown_tool_repeats = int(cfg.get("max_unknown_tool_repeats", 5) or 5)
    state.last_unknown_tool_name = None
    state.unknown_tool_repeat_count = 0
    state.last_batch_unknown_tool_count = 0
    while True:
        if check_stop is not None and check_stop():
            return None
        if get_pending_messages is not None:
            pending = get_pending_messages()
            if pending:
                messages = pending
        # Если от backend'а или get_pending_messages пришёл пустой список сообщений,
        # не передаём его дальше в check_trace_finished / check_mode, чтобы не падать
        # с ValueError('Empty messages list'). Вместо этого генерируем понятное
        # сообщение‑обратную связь для агента.
        if not messages:
            run_logger.error("Empty messages list")
            tool_error_cnt += 1
            messages = [
                _make_technical_feedback_user_message(
                    NO_TOOL_CALL_BOUNCE_MESSAGE,
                    technical_type=TECHNICAL_TYPE_TECHNICAL,
                )
            ]
            state.steps += 1
            continue
        mode_for_step_count = None
        mode = None
        # step += 1 только после завершённого хода модели (см. конец итерации).
        count_completed_step = False
        backend_mode = _get_backend_api_mode(ctx)
        try:
            for m in messages:
                m_formatted = format_message_responses(m)
                if console_human and len(m_formatted) > 0:
                    print(m_formatted)
                run_logger.info(
                    "\n<message>\n%s\n</message>\n",
                    json.dumps(m, ensure_ascii=False, indent=2),
                )
                run_logger.info(
                    "dialog_message",
                    extra={
                        "event": "dialog_message",
                        "payload": m,
                        "trace": m_formatted or None,
                    },
                )

            # В state и в тред уходит уже санитайзнутый набор (битые function_call ->
            # technical user message). Сырой вывод бэкенда логируется выше; если
            # санитайз изменил батч — логируем то, что реально закоммичено (иначе
            # в agent_log не видно подставленного фидбека, хотя он есть в threads jsonl).
            _pre_sanitize_digest = json.dumps(
                messages, ensure_ascii=False, sort_keys=True, default=str
            )
            messages = sanitize_messages_for_chat_completions(
                messages,
                invalid_tool_call_factory=build_invalid_tool_call_feedback_message,
            )
            _post_sanitize_digest = json.dumps(
                messages, ensure_ascii=False, sort_keys=True, default=str
            )
            if _post_sanitize_digest != _pre_sanitize_digest:
                run_logger.info(
                    "\n<message_batch_committed>\n"
                    "Санитайз для chat/completions изменил батч относительно сырого "
                    "ответа выше (например, невалидный JSON вызова тула заменён на "
                    "техническое user-сообщение — то же, что уходит в persist треда).\n"
                    "</message_batch_committed>\n"
                )
                for m in messages:
                    run_logger.info(
                        "\n<message_committed>\n%s\n</message_committed>\n",
                        json.dumps(m, ensure_ascii=False, indent=2, default=str),
                    )
            new_state_messages = filter_messages_not_in_state(
                list(getattr(state, "messages", []) or []),
                list(messages),
            )
            if new_state_messages:
                state.messages += new_state_messages
            await _maybe_persist(persist_messages_callback, messages, state)

            # Лимиты (step / tool_calls / tokens) — только когда нет pending
            # function_call: сначала дожимаем весь батч tools от модели, иначе
            # call уже в state/jsonl, а exec/sidecar/result теряются.
            # step = завершённый ход модели (после tool-batch / text), не half-turn.
            pending_tool_exec = _batch_has_pending_function_calls(messages)
            if not pending_tool_exec:
                if state.steps >= state.max_steps:
                    state.terminal_outcome = "step_limit"
                    return await _acall_blocking_hook(build_limit_envelope, state, "step_limit", ctx)

                if (
                    max_tool_calls_limit is not None
                    and state.tool_calls >= max_tool_calls_limit
                ):
                    state.terminal_outcome = "tool_call_limit"
                    return await _acall_blocking_hook(build_limit_envelope, state, "tool_call_limit", ctx)

                if session_token_limit is not None:
                    _baseline = UsageAccount.from_meta(
                        getattr(state, "run_start_usage_meta", None)
                    )
                    _spent = state.usage.delta_from(_baseline).totals()["total"]
                    if _spent >= session_token_limit:
                        state.terminal_outcome = "token_limit"
                        return await _acall_blocking_hook(build_limit_envelope, state, "token_limit", ctx)

            if tool_error_cnt > max_tool_errors:
                state.terminal_outcome = "tool_errors"
                return f'Agent tools call errors.'

            mode = check_mode(messages, run_logger)
            mode_for_step_count = mode
            if (
                backend_mode == "chat_completions"
                and _mode_requires_backend_call(mode)
                and pre_backend_compaction_hook is not None
            ):
                try:
                    await _acall_blocking_hook(pre_backend_compaction_hook, messages, mode)
                except Exception as pre_backend_exc:
                    run_logger.warning(
                        "Pre-backend compaction hook warning: %s",
                        pre_backend_exc,
                        exc_info=True,
                    )
            if (
                backend_mode == "chat_completions"
                and _mode_requires_backend_call(mode)
                and isinstance(session_meta, dict)
            ):
                try:
                    _messages_before_notice = len(messages)
                    messages = apply_tool_usage_notice_before_llm(
                        messages,
                        state,
                        ctx,
                        session_meta,
                        step_messages=messages,
                        logger=run_logger,
                    )
                    notice_messages = messages[_messages_before_notice:]
                    if notice_messages:
                        to_append = filter_messages_not_in_state(
                            list(getattr(state, "messages", []) or []),
                            list(notice_messages),
                        )
                        if to_append:
                            state.messages += to_append
                        if persist_messages_callback is not None:
                            try:
                                await await_callback(
                                    persist_messages_callback(notice_messages)
                                )
                            except Exception as persist_exc:
                                run_logger.warning(
                                    "Thread notice messages persist warning: %s",
                                    persist_exc,
                                    exc_info=True,
                                )
                except Exception as mem_exc:
                    run_logger.warning(
                        "Memory messages hook warning: %s",
                        mem_exc,
                        exc_info=True,
                    )
        except Exception as e:
            # Ошибки до успешного разбора батча (санитайз, check_mode, …) — bounce.
            run_logger.error(e, exc_info=True)
            tool_error_cnt += 1
            assistant_text = extract_last_assistant_text(messages)
            bounce_msg = format_no_tool_call_bounce(assistant_text)
            messages = [
                _make_technical_feedback_user_message(
                    bounce_msg,
                    technical_type=TECHNICAL_TYPE_TECHNICAL,
                )
            ]
            count_completed_step = True
        else:
            # Вызовы backend не ловим здесь: после исчерпания transport/adaptive
            # ретраев исключение уходит наверх как есть.
            try:
                if mode == "user_ask":
                    try:
                        messages, response_id = await aprocess_user_ask(messages, state, ctx)
                    except Exception as user_ask_exc:
                        can_retry_with_full_history = (
                            backend_mode == "responses"
                            and state.previous_response_id is not None
                            and get_full_history_messages is not None
                            and _is_previous_response_cache_miss(user_ask_exc)
                        )
                        if not can_retry_with_full_history:
                            raise

                        run_logger.warning(
                            "Cache miss for previous_response_id=%s. Retry with full history.",
                            state.previous_response_id,
                        )
                        full_history_messages = get_full_history_messages()
                        state.previous_response_id = None
                        messages, response_id, _ = await _arequest_backend_canonical(
                            full_history_messages,
                            state,
                            ctx,
                            set_previous_response_id=False,
                        )
                    if backend_mode == "responses":
                        state.previous_response_id = response_id
                    state.last_backend_response_id = response_id
                    # Half-turn: ответ модели обработаем на следующей итерации.
                elif mode == "function_call":
                    # Finish-turn handoff closes the CURRENT user turn only when we
                    # process fresh backend function_calls (mode == "function_call"),
                    # not when replaying older trace.
                    finish_response = check_trace_finished(messages)
                    if finish_response:
                        finish_arguments, finish_call_id = finish_response
                        grouped_messages = group_messages(messages)
                        pending_tool_calls = grouped_messages.get("tool_calls", [])
                        # Emit synthetic outputs for all pending tool_calls once (incl. finish):
                        # chat/completions needs tool rows after assistant.tool_calls.
                        await aappend_cycle_stop_tool_outputs_for_batch(
                            state=state,
                            ctx=ctx,
                            tool_calls=pending_tool_calls,
                            run_agent_func=arun_agent,
                            persist_messages_callback=persist_messages_callback,
                            backend_mode=backend_mode,
                        )
                        if backend_mode == "chat_completions":
                            gate = FinishGateGuard(ctx=ctx, state=state, logger=run_logger)
                            # Индикатор «сверяюсь…» — только когда судья реально пойдёт
                            # (gate.enabled). Smart-skip / strict=off / не-chat → без LLM,
                            # флаг не поднимаем (иначе ложный flash + лишний runtime-meta).
                            # Снимаем строго в finally, даже если evaluate бросит.
                            checking = (
                                mark_finish_gate_checking is not None and gate.enabled
                            )
                            if checking:
                                await await_callback(mark_finish_gate_checking(True))
                            try:
                                decision = await gate.aevaluate_turn_finish(
                                    finish_handoff_arguments=finish_arguments,
                                )
                            finally:
                                if checking:
                                    await await_callback(
                                        mark_finish_gate_checking(False)
                                    )
                            await apply_finish_gate_to_turn_finish_output_in_state(
                                state=state,
                                finish_call_id=finish_call_id,
                                decision=decision,
                                logger=run_logger,
                                persist_messages_callback=persist_messages_callback,
                            )
                            final_submit_output = get_function_call_output_text(
                                state.messages, finish_call_id
                            )
                            await _acall_blocking_hook(maybe_update_intent_scope_from_tool,
                                tool_name=FINISH_TOOL_NAME,
                                tool_output_raw=final_submit_output or "",
                                tool_arguments=finish_arguments or "",
                                state=state,
                                ctx=ctx,
                            )
                            if decision.outcome == "reject":
                                feedback = (decision.feedback_text or "").strip()
                                if not feedback and decision.validation is not None:
                                    feedback = build_finish_gate_feedback(
                                        decision.validation
                                    )
                                messages = [
                                    _make_technical_feedback_user_message(
                                        build_finish_gate_reject_technical_user_content(
                                            feedback
                                        ),
                                        technical_type=TECHNICAL_TYPE_TECHNICAL,
                                    )
                                ]
                                # Finish-reject — завершённый ход (batch + gate).
                                count_completed_step = True
                            else:
                                # accept / fail_open_error / fail_open_limit / прочее —
                                # все завершают ход одинаково.
                                state.steps += 1
                                state.flush_current_user_intent()
                                if turn_end_compaction_hook is not None:
                                    await _acall_blocking_hook(turn_end_compaction_hook)
                                # Сообщение, пришедшее во время (блокирующей) компакции, не
                                # теряем: продолжаем новым ходом на уже сжатом контексте.
                                if get_pending_messages is not None:
                                    pending = get_pending_messages()
                                    if pending:
                                        messages = pending
                                        continue
                                return finish_arguments
                        else:
                            state.current_user_intent = []
                            state.steps += 1
                            return finish_arguments
                    else:
                        messages = await aprocess_tool_calls(
                            messages, state, ctx, run_agent_func=arun_agent,
                            persist_messages_callback=persist_messages_callback,
                        )
                        n_unknown = int(
                            getattr(state, "last_batch_unknown_tool_count", 0) or 0
                        )
                        if n_unknown > 0:
                            tool_error_cnt += n_unknown
                        else:
                            tool_error_cnt = 0
                        if (
                            int(getattr(state, "unknown_tool_repeat_count", 0) or 0)
                            >= max_unknown_tool_repeats
                        ):
                            unknown_name = (
                                getattr(state, "last_unknown_tool_name", None) or "unknown"
                            )
                            state.terminal_outcome = "tool_errors"
                            return (
                                f"Agent stopped: repeated unknown tool '{unknown_name}'."
                            )
                        # Полный ход: LLM уже вернул tools, batch исполнен.
                        count_completed_step = True
                elif mode == "function_call_output":
                    messages, response_id = await aprocess_tool_outputs(messages, state, ctx)
                    if backend_mode == "responses":
                        state.previous_response_id = response_id
                    state.last_backend_response_id = response_id
                    # Half-turn: как user_ask — ждём разбора ответа модели.
                elif mode == "text_response":
                    assistant_text = extract_last_assistant_text(messages)
                    decision = await ashould_return_text_answer(ctx, state, assistant_text)
                    
                    if decision.accept:
                        # Финальная проза принята → TUI-индикатор «✓ завершаю…»
                        # на время записи/саммари/flush (mark_finalizing пишет
                        # runtime.finalizing в meta; снимаем строго в finally).
                        state.steps += 1
                        if mark_finalizing is not None:
                            await await_callback(mark_finalizing(True))
                        try:
                            # В поток, как соседние intent-scope хуки: внутри
                            # синхронный aux-LLM, и прямой вызов из корутины
                            # упирается в портал, а fail-open это глотает —
                            # сводка молча подменялась сырым текстом.
                            await _acall_blocking_hook(
                                maybe_record_final_answer_intent,
                                assistant_text=assistant_text, state=state, ctx=ctx
                            )
                            state.flush_current_user_intent()
                        finally:
                            if mark_finalizing is not None:
                                await await_callback(mark_finalizing(False))
                        if turn_end_compaction_hook is not None:
                            await _acall_blocking_hook(turn_end_compaction_hook)
                        if get_pending_messages is not None:
                            pending = get_pending_messages()
                            if pending:
                                messages = pending
                                continue
                        return assistant_text
                    # === reject ===
                    tool_error_cnt += 1
                    if decision.reject_feedback is not None:      # judge-reject c причиной
                        bounce_msg = decision.reject_feedback
                    else:                                         # deny / пусто / нет гейта / ошибка
                        run_logger.warning(
                            "Agent returned text response without tool call, requesting tool call"
                        )
                        bounce_msg = format_no_tool_call_bounce(assistant_text)
                    messages = [
                        _make_technical_feedback_user_message(
                            bounce_msg,
                            technical_type=TECHNICAL_TYPE_TECHNICAL,
                        )
                    ]
                    count_completed_step = True
                else:
                    raise ValueError(f"unconventional mode: {mode}")
            except WrongFunctionArgumentsException as e:
                run_logger.error(e, exc_info=True)
                tool_error_cnt += 1
                # Если finish_reason=="length" — JSON был усечён потолком
                # max_tokens. Объясняем модели причину и советуем компактный
                # вариант/разбиение на вызовы; иначе — feedback как есть.
                if getattr(state, "last_finish_reason", None) == "length":
                    e.extra_hint = (
                        "⚠ Output was truncated: output token limit reached "
                        "(finish_reason == \"length\"). Shorten the response "
                        "or split it into multiple calls with smaller payloads."
                    )
                messages = [
                    _make_technical_feedback_user_message(
                        e.to_llm_feedback(),
                        technical_type=TECHNICAL_TYPE_ERROR_MESSAGE,
                        call_id=getattr(e, "call_id", None),
                    )
                ]
                # Аргументы отвалились на exec — ход модели всё равно «закрыт».
                count_completed_step = True
            except Exception as backend_exc:
                # Переполнение контекстного окна модели → не крашимся: форс-сжатие
                # истории (безусловное, как /compact) и повтор запроса со сжатым
                # контекстом. Лимит попыток — если даже сжатие не помогает, уходим
                # наверх. Прочие ошибки — как раньше, наверх без изменений.
                if (
                    force_compact_hook is not None
                    and is_context_overflow_error(backend_exc)
                    and context_overflow_retries < _MAX_CONTEXT_OVERFLOW_RETRIES
                ):
                    context_overflow_retries += 1
                    run_logger.warning(
                        "Context overflow → force-compaction + retry (%s/%s): %s",
                        context_overflow_retries,
                        _MAX_CONTEXT_OVERFLOW_RETRIES,
                        backend_exc,
                    )
                    await _acall_blocking_hook(force_compact_hook)
                    continue
                raise

        # step += 1 только после завершённого хода модели (tool-batch / text /
        # bounce / pre-mode error). user_ask и function_call_output — half-turn
        # к LLM, step не тратят. skip_initial: resume `-t` не считает первый ask.
        skip_initial_step = bool(
            getattr(state, "skip_initial_user_ask_step_when_history", False)
        )
        if skip_initial_step and state.steps == 0 and mode_for_step_count == "user_ask":
            pass
        elif count_completed_step:
            state.steps += 1


# --- синхронные двойники --------------------------------------------------
#
# Реализация у каждой ступени цикла одна — асинхронная. Здесь только портал:
# сигнатуры, блокирующая семантика и возвращаемые значения прежние, поэтому
# agent_executor, тесты и встраивающий код правок не требуют.


@functools.wraps(_arequest_backend_canonical)
def _request_backend_canonical(*args: Any, **kwargs: Any):
    return get_portal().run(_arequest_backend_canonical(*args, **kwargs))


@functools.wraps(aprocess_user_ask)
def process_user_ask(*args: Any, **kwargs: Any):
    return get_portal().run(aprocess_user_ask(*args, **kwargs))


@functools.wraps(aprocess_tool_calls)
def process_tool_calls(*args: Any, **kwargs: Any):
    return get_portal().run(aprocess_tool_calls(*args, **kwargs))


@functools.wraps(aprocess_tool_outputs)
def process_tool_outputs(*args: Any, **kwargs: Any):
    return get_portal().run(aprocess_tool_outputs(*args, **kwargs))


@functools.wraps(arun_agent)
def run_agent(*args: Any, **kwargs: Any) -> Union[str, None]:
    """Синхронный прогон цикла агента.

    Контракт не изменился: вызов блокирует поток до конца хода и возвращает то
    же, что раньше.
    """
    return get_portal().run(arun_agent(*args, **kwargs))
