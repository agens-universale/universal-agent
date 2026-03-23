import json

from tools import apply_tool, WrongFunctionArgumentsException
from utils.connect_backend import create_call_headers
from utils.connect_backend import request_backend_responses as request_backend
from utils.connect_backend import create_call_data_responses as create_call_data
from utils.format_messages import format_message_responses
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func

from states import State
from typing import List, Dict, Union, Tuple, Any, Callable, Optional

import logging

_fallback_logger = logging.getLogger(__name__)


def _is_previous_response_cache_miss(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "previous_response_id" in text
        and ("not found" in text or "cache" in text)
    ) or ("cache" in text and "not found" in text)


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


def check_trace_finished(messages: List[Dict]) -> Union[str, None]:
    if len(messages) == 0:
        raise ValueError('Empty messages list')

    grouped_messages = group_messages(messages)
    for m in grouped_messages['tool_calls']:
        if m['name'] == 'submit_solution':
            return m['arguments']

    return None


def process_user_ask(messages, state: State, ctx: Any):
    message = messages[-1]
    messages_to_send = [message]

    call_data = create_call_data(messages, state)

    call_headers = create_call_headers(state, ctx)
    cfg = getattr(ctx, "config_dict", {}) or {}
    verify = cfg.get("use_ssl", True)
    use_retry = cfg.get("use_retry", False)
    url = cfg["agent_url"]
    res, res_headers = request_backend(
        call_data,
        call_headers,
        url=url,
        verify=verify,
        use_retry=use_retry,
        logger=getattr(ctx, "logger", None),
    )
    state.set_resp_headers_metadata(res_headers)
    resp_messages = res['output']

    return resp_messages, res["id"]


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
            parent_state=state,
            run_agent_func=run_agent_func,
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
    call_headers = create_call_headers(state, ctx)
    call_data = create_call_data(messages, state)

    # logger.debug('process_tool_outputs data to ask backend')
    # logger.debug(json.dumps(call_data, ensure_ascii=False, indent=2))
    cfg = getattr(ctx, "config_dict", {}) or {}
    verify = cfg.get("use_ssl", True)
    use_retry = cfg.get("use_retry", False)
    url = cfg["agent_url"]
    res, res_headers = request_backend(
        call_data,
        call_headers,
        url=url,
        verify=verify,
        use_retry=use_retry,
        logger=getattr(ctx, "logger", None),
    )
    state.set_resp_headers_metadata(res_headers)

    resp_messages = res["output"]
    return resp_messages, res["id"]


def run_agent(
    messages,
    state: State,
    ctx: Any,
    *,
    get_full_history_messages: Optional[Callable[[], List[Dict]]] = None,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]] = None,
    get_pending_messages: Optional[Callable[[], List[Dict]]] = None,
    check_stop: Optional[Callable[[], bool]] = None,
) -> Union[str, None]:
    tool_error_cnt = 0
    run_logger = getattr(ctx, "logger", _fallback_logger)
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
                {
                    "role": "user",
                    "content": "There is no tool call in Agent response. Please send appropriate tool call",
                    "id": generate_message_id_func("m"),
                }
            ]
            state.steps += 1
            continue
        try:
            for m in messages:
                m_formatted = format_message_responses(m)
                if console_trace and len(m_formatted) > 0:
                    print(m_formatted)
                run_logger.info(
                    "\n<message>\n%s\n</message>\n",
                    json.dumps(m, ensure_ascii=False, indent=2),
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
            finish_response = check_trace_finished(messages)

            if finish_response:
                return finish_response

            if state.steps > state.max_steps:
                return f'Agent executed {state.max_steps} steps and doesn\'t finish his work because of step limits.'

            if tool_error_cnt > max_tool_errors:
                return f'Agent tools call errors.'

            mode = check_mode(messages, run_logger)
            if mode == "user_ask":
                try:
                    messages, response_id = process_user_ask(messages, state, ctx)
                except Exception as user_ask_exc:
                    can_retry_with_full_history = (
                        state.previous_response_id is not None
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
                    messages, response_id = process_user_ask(
                        full_history_messages, state, ctx
                    )
                state.previous_response_id = response_id
            elif mode == "function_call":
                messages = process_tool_calls(messages, state, ctx, run_agent_func=run_agent)
                tool_error_cnt = 0
            elif mode == "function_call_output":
                messages, response_id = process_tool_outputs(messages, state, ctx)
                state.previous_response_id = response_id
            else:
                raise ValueError(f"unconventional mode: {mode}")
        except WrongFunctionArgumentsException as e:
            run_logger.error(e, exc_info=True)
            tool_error_cnt += 1
            messages = [
                {
                    "role": "user",
                    "content": e.to_llm_feedback(),
                    "id": generate_message_id_func("m"),
                }
            ]
        except Exception as e:
            # raise e
            run_logger.error(e, exc_info=True)
            tool_error_cnt += 1
            messages = [
                {
                    "role": "user",
                    "content": "There is no tool call in Agent response. Please send appropriate tool call",
                    "id": generate_message_id_func("m"),
                }
            ]

        state.steps += 1
