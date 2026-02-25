import json
from copy import deepcopy

from utils.convertations import output_chat_completion_to_response

cut_message = 350


def cut_message_f(s):
    s = str(s)
    return s if len(str(s)) < cut_message else str(s)[:cut_message] + '\n...[cutted]'


def format_type_message(message):
    role = message.get('role', 'Unknown role')
    content = message.get('content', [])

    if 'role' == 'system':
        return ''

    tmpl = ''

    if isinstance(content, str):
        if len(content) == 0:
            return ''

        tmpl = f'\n\n>>>>>>>>>>\n{role}: {content}\n'
        return tmpl

    parts = []
    if isinstance(content, list):
        for m in content:
            text = m.get('text', '')
            if len(text) > 0:
                parts.append(text)
        parts_text = '\n\n'.join(parts)

        if len(parts_text) > 0:
            tmpl = f'\n\n>>>>>>>>>>\n{role}: {parts_text}\n'
            return tmpl
        else:
            return ''
    else:
        return json.dumps(message, ensure_ascii=False, indent=2)


def format_type_functions_call(message):
    tmpl = f"\n\n>>>>>>>>>>\ncall_id: {message['call_id']}:\n{message['name']}\n"
    try:
        args = []
        for k,v in json.loads(message['arguments']).items():
            v_ = cut_message_f(v)
            args.append(f'{k}: {v_}')
        tmpl += '\n'.join(args)
    except Exception as e:
        tmpl += str(e) + '\n'
        tmpl += cut_message_f(message)
    return tmpl


def format_type_function_call_output(message):
    if 'output' in message:
        text = message['output']
    else:
        text = message['content']
    text = str(text)
    output = cut_message_f(text)
    tmpl = f"\n\n>>>>>>>>>>\ncompleted: {message['call_id']}:\n{output}\n"
    return tmpl


def format_others_responses(message):
    if message.get('role') == 'system':
        return '\n\n>>>>>>>>>>\nsystem: <local system prompt. See debug logs>'
    if message.get('role') == 'user':
        return f'\n\n>>>>>>>>>>\nuser: {message["content"]}'
    tmpl = '\n\n>>>>>>>>>>\n' + json.dumps(message, ensure_ascii=False, indent=2)
    return tmpl


def format_message_responses(message):
    m_type = message.get('type', None)
    if m_type == 'message':
        return format_type_message(message)
    elif m_type == 'function_call_output':
        return format_type_function_call_output(message)
    elif m_type == 'function_call':
        return format_type_functions_call(message)
    else:
        return format_others_responses(message)


def format_message_completions(message):
    m_type = message.get('role', None)
    if m_type == 'user':
        return format_type_message(message)
    elif m_type == 'tool':
        return format_type_function_call_output(message)
    elif m_type == 'assistant':
        tool_calls = []
        for t in message.get('tool_calls', []):
            d = deepcopy(t['function'])
            d['call_id'] = t['id']
            tool_calls.append(d)
        parts = [format_type_message(message)]
        for t in tool_calls:
            parts.append(format_type_functions_call(t))
        return '\n\n'.join(parts)
    else:
        return format_others_responses(message)


def format_message_chat_completions_to_send_backend(message):
    m_role = message.get('role')
    if m_role == 'tool':
        nm = dict()
        nm['role'] = 'tool'
        nm['tool_call_id'] = message['call_id']
        nm['content'] = message['content']
        return nm

    if m_role == 'system':
        nm = dict()
        nm['role'] = 'system'
        nm['content'] = message['content']
        return nm

    if m_role == 'user':
        nm = dict()
        nm['role'] = 'user'
        nm['content'] = message['content']
        return nm

    if m_role == 'assistant':
        nm = dict()
        nm['role'] = 'assistant'
        nm['content'] = deepcopy(message['content'])
        nm['tool_calls'] = deepcopy(message.get('tool_calls', list()))
        return nm

    return message
