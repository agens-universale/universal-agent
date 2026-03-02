import json

import config
from tools import apply_tool, WrongFunctionArgumentsException
from utils.connect_backend import create_call_headers


from utils.connect_backend import request_backend_responses as request_backend
from utils.connect_backend import create_call_data_responses as create_call_data
from utils.format_messages import format_message_responses
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func

from states import State
from typing import List, Dict, Union, Tuple

import logging

logger = logging.getLogger(__name__)


def check_mode(messages) -> Union[str, None]:
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


def process_user_ask(messages, state):

    message = messages[-1]
    messages_to_send = [message]

    call_data = create_call_data(messages, state)

    call_headers = create_call_headers(state)
    res, res_headers = request_backend(call_data, call_headers)
    state.set_resp_headers_metadata(res_headers)
    resp_messages = res['output']

    return resp_messages, res['id']


def process_tool_calls(messages: List[Dict], state: State):
    grouped_messages = group_messages(messages)
    if len(grouped_messages['tool_outputs']) > 0:
        raise ValueError('Messages has tool_outputs')

    tools_outputs = []
    for m in grouped_messages['tool_calls']:
        tool_res = apply_tool(tool_name=m['name'], arguments=m['arguments'], parent_state=state, run_agent_func=run_agent)
        new_message = {
            'id': generate_message_id_func('fco'),
            'type': 'function_call_output',
            'call_id': m['call_id'],
            'output': tool_res,
        }

        tools_outputs.append(new_message)

    return tools_outputs


def process_tool_outputs(messages, state):
    call_headers = create_call_headers(state)
    call_data = create_call_data(messages, state)

    # logger.debug('process_tool_outputs data to ask backend')
    # logger.debug(json.dumps(call_data, ensure_ascii=False, indent=2))
    res, res_headers = request_backend(call_data, call_headers)
    state.set_resp_headers_metadata(res_headers)

    resp_messages = res['output']
    return resp_messages, res['id']


def run_agent(messages, state: State) -> Union[str, None]:
    tool_error_cnt = 0
    while True:
        try:
            for m in messages:
                m_formatted = format_message_responses(m)
                if len(m_formatted) > 0:
                    print(m_formatted)
                logger.info(f'\n<message>\n{json.dumps(m, ensure_ascii=False, indent=2)}\n</message>\n')

            state.messages += messages
            finish_response = check_trace_finished(messages)

            if finish_response:
                return finish_response

            if state.steps > state.max_steps:
                return f'Agent executed {state.max_steps} steps and doesn\'t finish his work because of step limits.'

            if tool_error_cnt > config.config_dict['no_tool_call_max_count']:
                return f'Agent tools call errors.'

            mode = check_mode(messages)
            if mode == 'user_ask':
                messages, response_id = process_user_ask(messages, state)
                state.previous_response_id = response_id
            elif mode == 'function_call':
                messages = process_tool_calls(messages, state)
                tool_error_cnt = 0
            elif mode == 'function_call_output':
                messages, response_id = process_tool_outputs(messages, state)
                state.previous_response_id = response_id
            else:
                raise ValueError(f'unconventional mode: {mode}')
        except WrongFunctionArgumentsException as e:
            logger.error(e, exc_info=True)
            tool_error_cnt += 1
            messages = [
                {
                    'role': 'user',
                    'content': e.to_llm_feedback(),
                    'id': generate_message_id_func('m')
                }
            ]
        except Exception as e:
            # raise e
            logger.error(e, exc_info=True)
            tool_error_cnt += 1
            messages = [
                {
                    'role': 'user',
                    'content': 'There is no tool call in Agent response. Please send appropriate tool call',
                    'id': generate_message_id_func('m')
                }
            ]

        state.steps += 1
