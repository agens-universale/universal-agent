from copy import deepcopy

import logging
import requests
import re
import json
from uuid import uuid4
from typing import List, Dict, Optional, Tuple


from utils.convertations import output_chat_completion_to_response
from utils.format_messages import format_message_chat_completions_to_send_backend
from utils.retry import retry_with_backoff


logger = logging.getLogger(__name__)


def _default_should_retry_backend_exception(exc: Exception) -> bool:
    """
    Ретраим только вероятно транзиентные ошибки:
    - сетевые ошибки/таймауты
    - HTTP 429 и 5xx
    - иногда рвётся стрим: ошибки парсинга/валидации
    """
    if isinstance(exc, requests.exceptions.HTTPError):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code is None:
            return False
        return status_code == 429 or status_code >= 500

    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.RequestException,
        ),
    ):
        return True

    return isinstance(exc, (json.JSONDecodeError, ValueError))


@retry_with_backoff(
    should_retry=_default_should_retry_backend_exception,
    retry_log_level=logging.ERROR,
    no_more_retries_log_level=logging.ERROR,
    operation_name="Backend request",
)
def request_backend_responses(
    data: Dict,
    headers: Dict,
    url: str,
    verify: bool = True,
) -> Tuple[Dict, Dict]:
    """
    Отправить запрос в backend responses API.

    url и verify передаются явно вызывающим кодом (как правило, из ctx.config_dict).
    """
    chunks: List[bytes] = []

    data['stream'] = True

    with requests.post(url, json=data, headers=headers, stream=True, verify=verify) as r:
        r.raise_for_status()
        r_headers = r.headers
        for line in r.iter_lines():
            chunks.append(line)
            # print(line.decode('utf-8'))

    last_chunk = chunks[-1].decode('utf-8')
    # print(last_chunk)
    # print(last_chunk)
    try:
        result = json.loads(re.sub('^data: +', '', last_chunk))
    except Exception as e:
        print(f'Error read json from backend. Last chunk: {last_chunk}')
        raise e

    result_type = result.get("type")
    if result_type != "response.completed":
        raise ValueError(f'wrong stream finish: {chunks[-1].decode("utf-8")}')

    return result["response"], r_headers


@retry_with_backoff(
    should_retry=_default_should_retry_backend_exception,
    retry_log_level=logging.ERROR,
    no_more_retries_log_level=logging.ERROR,
    operation_name="Backend chat completion request",
)
def request_backend_chat_completions(data: Dict, headers: Dict, url: str, verify: bool = True):
    """
    Отправить запрос в backend chat completions API.

    url передаётся явно вызывающим кодом (как правило, из ctx.config_dict).
    """
    r = requests.post(url, json=data, headers=headers, verify=verify)
    r.raise_for_status()
    # res = output_chat_completion_to_response(r.json())
    return r.json()


def create_call_headers(state, ctx) -> Dict[str, str]:
    """
    Сформировать заголовки вызова для backend API на основе State и RunContext.
    """
    config_dict = getattr(ctx, "config_dict", {}) or {}
    auth_connector = getattr(ctx, "auth_connector", None)

    call_headers: Dict[str, str] = {
        "x_cl_id": str(config_dict.get("x_cl_id")),
        "x_trace_id": str(state.state_id),
        "x_rq_id": str(uuid4()),
        "x-session-id": str(state.state_id),
    }
    if state.x_target is not None:
        call_headers["x-target"] = state.x_target
    if auth_connector is not None:
        token = auth_connector.get_token()
        call_headers["Authorization"] = f"Bearer {token}"
    return call_headers


def create_call_data_responses(
    messages,
    state,
    tools: Optional[List] = None,
    set_previous_response_id: bool = True,
) -> Dict:
    call_data = dict()
    call_data["model"] = state.default_model
    call_data["input"] = messages
    call_data["tools"] = tools if tools else state.tools
    call_data["metadata"] = state.call_metadata
    call_data["previous_response_id"] = (
        state.previous_response_id if set_previous_response_id else None
    )
    return call_data

    
def create_call_data_completions(
    messages,
    state: Optional = None,
    tools: Optional[List] = None,
    model: Optional[str] = None,
    *args,
    **kwargs,
) -> Dict:
    if state is not None:
        state_messages = deepcopy(state.messages)
        state_tools = deepcopy(state.tools)
    else:
        state_messages = []
        state_tools = []

    if isinstance(state_messages, list) and len(state_messages) > 0:
        to_send_messages = state_messages
        msgs_ids = set((m['id'] for m in state_messages))
        for m in messages:
            if m['id'] not in msgs_ids:
                to_send_messages.append(m)
    else:
        to_send_messages = messages

    to_send_tools = tools if tools is not None else state_tools

    to_send_messages_formatted = []
    for m in to_send_messages:
        to_send_messages_formatted.append(
            format_message_chat_completions_to_send_backend(m)
        )

    effective_model = model
    if effective_model is None and state is not None:
        effective_model = state.default_model

    data = {
        "model": effective_model,
        "messages": to_send_messages_formatted,
        "tools": to_send_tools,
        "temperature": 0.7,
        "tool_choice": "auto",
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "max_tokens": 8096,
        "repetition_penalty": 1.2,
        "separate_reasoning": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    return data
    
if __name__ == "__main__":
    ...
