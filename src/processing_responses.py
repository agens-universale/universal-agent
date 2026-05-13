import json
from types import SimpleNamespace

from config import parse_chat_completions_max_tokens
from intent_guard import validate_final_report_vs_user_intent
from message_policy.cycle_stop import append_cycle_stop_tool_outputs_for_batch
from request_backend_policy import request_chat_completions_with_policy
from tools import apply_tool, WrongFunctionArgumentsException
from tools.stored_outputs.wrapping import maybe_store_and_wrap_tool_output
from utils.connect_backend import create_call_headers
from utils.connect_backend import request_backend_responses as request_backend
from utils.connect_backend import create_call_data_responses as create_call_data
from utils.connect_backend import create_call_data_completions
from utils.convertations import (
    output_chat_completion_to_response,
    sanitize_messages_for_chat_completions,
)
from utils.format_messages import format_message_responses
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func

from states import State
from typing import List, Dict, Union, Tuple, Any, Callable, Optional
from message_policy.user_message_policy import (
    TECHNICAL_TYPE_TECHNICAL,
    TECHNICAL_TYPE_ERROR_MESSAGE,
    wrap_technical_user_content,
    build_invalid_tool_call_feedback_message,
)

import logging

_fallback_logger = logging.getLogger(__name__)

NO_TOOL_CALL_BOUNCE_MESSAGE = (
    "There is no tool call in your response. The user does NOT see prose "
    "written outside tool calls — only tool calls render to the user. "
    "Retry with one of:\n"
    "  (1) If you intended an ACTION (read files, explore, search, write, etc.), "
    "call the appropriate tool NOW in this turn.\n"
    "  (2) If you intended to ASK THE USER (present options, get a decision), "
    "call ask_human(question=...) and put the FULL preceding content (tables, "
    "diagrams, architectures, options, recommendations) INSIDE the question "
    "parameter. Do not strip — the user must see what you wrote."
)


def _extract_last_assistant_text(messages: List[Dict[str, Any]]) -> str:
    """
    Return concatenated text from the last assistant `message` in the batch.
    Returns empty string if there is no assistant message or the content is
    empty/whitespace-only. Handles both string content and list-of-parts.
    """
    if not messages:
        return ""
    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("type") == "message"
            and message.get("role") == "assistant"
        ):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts: List[str] = []
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text)
                return "\n\n".join(parts).strip()
            return ""
    return ""


def _format_no_tool_call_bounce(assistant_text: str) -> str:
    """
    Build the no-tool-call bounce message. If the assistant's just-emitted
    text is available, inline it in a copy-paste-ready block so the model
    doesn't have to recompose from memory. Otherwise fall back to the static
    NO_TOOL_CALL_BOUNCE_MESSAGE constant.
    """
    text = (assistant_text or "").strip()
    if not text:
        return NO_TOOL_CALL_BOUNCE_MESSAGE
    return (
        "There is no tool call in your response. Here is what you just "
        "wrote, which the user did NOT see (prose outside a tool call is "
        "invisible):\n\n"
        "<<<\n"
        f"{text}\n"
        ">>>\n\n"
        "Retry with one of:\n"
        "  (1) If you intended an ACTION (read files, explore, search, "
        "write, etc.), call the appropriate tool NOW in this turn.\n"
        "  (2) If you intended to ASK THE USER, call ask_human(question=...) "
        "and paste the ENTIRE block above (between <<< and >>>) into the "
        "question parameter VERBATIM. Do not paraphrase, do not summarize, "
        "do not strip — copy as-is. Append any short clarifying question "
        "after the pasted block if needed."
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


def _make_technical_feedback_user_message(
    content: str, *, technical_type: str = TECHNICAL_TYPE_TECHNICAL
) -> Dict[str, str]:
    return {
        "role": "user",
        "content": wrap_technical_user_content(
            content, technical_type=technical_type
        ),
        "id": generate_message_id_func("m"),
    }


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


def _request_backend_canonical(
    messages: List[Dict],
    state: State,
    ctx: Any,
    *,
    set_previous_response_id: bool = True,
) -> Tuple[List[Dict], str, Dict]:
    """
    Унифицированный вызов backend для двух режимов API.

    Возвращает:
    - канонические сообщения (responses-like),
    - backend response id,
    - backend response headers (может быть пустым dict).
    """
    call_headers = create_call_headers(state, ctx)
    cfg = getattr(ctx, "config_dict", {}) or {}
    verify = cfg.get("use_ssl", True)
    use_retry = cfg.get("use_retry", False)
    backend_mode = _get_backend_api_mode(ctx)

    if backend_mode == "responses":
        url = cfg["agent_url"]
        call_data = create_call_data(
            messages,
            state,
            set_previous_response_id=set_previous_response_id,
        )
        res, res_headers = request_backend(
            call_data,
            call_headers,
            url=url,
            verify=verify,
            use_retry=use_retry,
            logger=getattr(ctx, "logger", None),
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

    res, call_data = request_chat_completions_with_policy(
        payload_factory=_create_payload,
        initial_max_tokens=completions_max_tokens,
        min_response_tokens=min_response_tokens,
        headers=call_headers,
        url=url,
        verify=verify,
        use_retry=use_retry,
        logger=getattr(ctx, "logger", None),
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

    logger.debug('Got Undefined mode for messages:')
    logger.debug(str(messages))

    raise ValueError('Undefined mode')


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
        if m['name'] == 'submit_solution':
            return m['arguments'], m['call_id']

    return None


def process_user_ask(messages, state: State, ctx: Any):
    resp_messages, response_id, res_headers = _request_backend_canonical(
        messages,
        state,
        ctx,
    )
    state.set_resp_headers_metadata(res_headers or {})
    return resp_messages, response_id


def process_tool_calls(
    messages: List[Dict],
    state: State,
    ctx: Any,
    run_agent_func,
):
    grouped_messages = group_messages(messages)
    if len(grouped_messages['tool_outputs']) > 0:
        raise ValueError('Messages has tool_outputs')

    tools_outputs = []
    for m in grouped_messages["tool_calls"]:
        tool_res = apply_tool(
            tool_name=m["name"],
            arguments=m["arguments"],
            ctx=ctx,
            state=state,
            run_agent_func=run_agent_func,
        )
        tool_res = maybe_store_and_wrap_tool_output(
            tool_res,
            call_id=m["call_id"],
            tool_name=m["name"],
            state=state,
            config=getattr(ctx, "config_dict", {}) or {},
            logger=getattr(ctx, "logger", None),
        )
        new_message = {
            "id": generate_message_id_func("fco"),
            "type": "function_call_output",
            "call_id": m["call_id"],
            "output": tool_res,
        }

        tools_outputs.append(new_message)

    return tools_outputs


def process_tool_outputs(messages, state: State, ctx: Any):
    resp_messages, response_id, res_headers = _request_backend_canonical(
        messages,
        state,
        ctx,
    )
    state.set_resp_headers_metadata(res_headers or {})
    return resp_messages, response_id


def run_agent(
    messages,
    state: State,
    ctx: Any,
    *,
    get_full_history_messages: Optional[Callable[[], List[Dict]]] = None,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]] = None,
    pre_backend_compaction_hook: Optional[Callable[[List[Dict], str], None]] = None,
    get_pending_messages: Optional[Callable[[], List[Dict]]] = None,
    check_stop: Optional[Callable[[], bool]] = None,
) -> Union[str, None]:
    tool_error_cnt = 0
    run_logger = getattr(ctx, "logger", None) or _fallback_logger
    cfg = getattr(ctx, "config_dict", {}) or {}
    console_trace = cfg.get("console_trace", True)
    max_tool_errors = cfg.get("no_tool_call_max_count", 0)
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
        try:
            for m in messages:
                m_formatted = format_message_responses(m)
                if console_trace and len(m_formatted) > 0:
                    print(m_formatted)
                run_logger.info(
                    "\n<message>\n%s\n</message>\n",
                    json.dumps(m, ensure_ascii=False, indent=2),
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
            state.messages += messages
            if persist_messages_callback is not None:
                try:
                    persist_messages_callback(messages)
                except Exception as persist_exc:
                    run_logger.warning(
                        "Thread messages persist warning: %s",
                        persist_exc,
                        exc_info=True,
                    )

            if state.steps > state.max_steps:
                return f'Agent executed {state.max_steps} steps and doesn\'t finish his work because of step limits.'

            if tool_error_cnt > max_tool_errors:
                return f'Agent tools call errors.'

            mode = check_mode(messages, run_logger)
            mode_for_step_count = mode
            backend_mode = _get_backend_api_mode(ctx)
            if (
                backend_mode == "chat_completions"
                and _mode_requires_backend_call(mode)
                and pre_backend_compaction_hook is not None
            ):
                try:
                    pre_backend_compaction_hook(messages, mode)
                except Exception as pre_backend_exc:
                    run_logger.warning(
                        "Pre-backend compaction hook warning: %s",
                        pre_backend_exc,
                        exc_info=True,
                    )
            if mode == "user_ask":
                try:
                    messages, response_id = process_user_ask(messages, state, ctx)
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
                    messages, response_id, _ = _request_backend_canonical(
                        full_history_messages,
                        state,
                        ctx,
                        set_previous_response_id=False,
                    )
                if backend_mode == "responses":
                    state.previous_response_id = response_id
                state.last_backend_response_id = response_id
            elif mode == "function_call":
                # `submit_solution` должен завершать ТЕКУЩИЙ user turn,
                # а не сразу срабатывать по истории предыдущих тактов.
                # Поэтому проверяем только на этапе обработки новых function_call
                # (которые пришли от бэкенда), когда mode == "function_call".
                finish_response = check_trace_finished(messages)
                if finish_response:
                    finish_arguments, finish_call_id = finish_response
                    grouped_messages = group_messages(messages)
                    pending_tool_calls = grouped_messages.get("tool_calls", [])
                    # Один раз закрываем pending tool_calls (в т.ч. submit_solution):
                    # chat/completions требует role=tool после assistant.tool_calls; для
                    # других backend_mode функция no-op.
                    append_cycle_stop_tool_outputs_for_batch(
                        state=state,
                        ctx=ctx,
                        tool_calls=pending_tool_calls,
                        run_agent_func=run_agent,
                        persist_messages_callback=persist_messages_callback,
                        backend_mode=backend_mode,
                    )
                    strict_finish_gate = bool(
                        cfg.get("strict_submit_solution_validation", True)
                    )
                    is_main_agent = getattr(state, "parent_state_id", None) is None
                    if strict_finish_gate and is_main_agent and backend_mode == "chat_completions":
                        active_user_messages = (
                            getattr(state, "current_user_messages", []) or []
                        )
                        full_user_messages_log = (
                            getattr(state, "user_messages_log", []) or []
                        )
                        if not active_user_messages:
                            active_user_messages = full_user_messages_log
                        context_user_messages = [
                            msg
                            for msg in full_user_messages_log
                            if msg not in set(active_user_messages)
                        ]
                        try:
                            validation = validate_final_report_vs_user_intent(
                                ctx=ctx,
                                state=state,
                                current_user_requests=active_user_messages,
                                context_user_messages=context_user_messages,
                                submit_solution_arguments=finish_arguments,
                            )
                        except Exception as validation_exc:
                            run_logger.warning(
                                "finish_gate_validation_failed; allow submit_solution "
                                "to avoid inconsistent tool_call history: %s",
                                validation_exc,
                                exc_info=True,
                            )
                            validation = None
                        if validation is None:
                            if is_main_agent:
                                state.clear_current_user_messages()
                            return finish_arguments
                        if not validation.ok:
                            run_logger.warning(
                                "finish_gate_rejected conflicts=%s missing=%s",
                                validation.conflicts,
                                validation.missing_requirements,
                            )
                            messages = [
                                _make_technical_feedback_user_message(
                                    validation.feedback_for_agent,
                                    technical_type=TECHNICAL_TYPE_TECHNICAL,
                                )
                            ]
                            continue
                    if is_main_agent:
                        state.clear_current_user_messages()
                    return finish_arguments
                messages = process_tool_calls(messages, state, ctx, run_agent_func=run_agent)
                tool_error_cnt = 0
            elif mode == "function_call_output":
                messages, response_id = process_tool_outputs(messages, state, ctx)
                if backend_mode == "responses":
                    state.previous_response_id = response_id
                state.last_backend_response_id = response_id
            else:
                raise ValueError(f"unconventional mode: {mode}")
        except WrongFunctionArgumentsException as e:
            run_logger.error(e, exc_info=True)
            tool_error_cnt += 1
            messages = [
                _make_technical_feedback_user_message(
                    e.to_llm_feedback(),
                    technical_type=TECHNICAL_TYPE_ERROR_MESSAGE,
                )
            ]
        except Exception as e:
            # raise e
            run_logger.error(e, exc_info=True)
            tool_error_cnt += 1
            assistant_text = _extract_last_assistant_text(messages)
            bounce_msg = _format_no_tool_call_bounce(assistant_text)
            messages = [
                _make_technical_feedback_user_message(
                    bounce_msg,
                    technical_type=TECHNICAL_TYPE_TECHNICAL,
                )
            ]

        # На первом `user_ask`-цикле при продолжении диалога по `-t` мы
        # отправляем бэкенду всю историю треда. Чтобы старые сообщения
        # не "сдвигали" контроль лимита шагов, пропускаем инкремент
        # `state.steps` только в этом случае.
        skip_initial_step = bool(
            getattr(state, "skip_initial_user_ask_step_when_history", False)
        )
        if not (skip_initial_step and state.steps == 0 and mode_for_step_count == "user_ask"):
            state.steps += 1
