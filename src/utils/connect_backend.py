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


def normalize_chat_completions_tools(
    tools: Optional[List],
) -> List[Dict]:
    """
    Привести tools-spec в формат, ожидаемый backend'ом chat/completions.

    Backend ожидает, что каждый tool выглядит так:
      {"type": "function", "function": {"name": ..., "parameters": ..., ...}}

    На клиенте tools часто хранятся "плоскими":
      {"type":"function","name":...,"parameters":...,"strict":true,...}
    """
    if not isinstance(tools, list):
        return []

    normalized: List[Dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue

        if tool.get("type") == "function" and "function" not in tool:
            fn: Dict = {}
            if "name" in tool:
                fn["name"] = tool.get("name")
            if "description" in tool:
                fn["description"] = tool.get("description")
            if "parameters" in tool:
                fn["parameters"] = tool.get("parameters")
            if "strict" in tool:
                fn["strict"] = tool.get("strict")

            normalized.append({"type": "function", "function": fn})
            continue

        normalized.append(tool)

    return normalized


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
    use_retry: bool = True,
    logger: Optional[logging.Logger] = None,
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
def request_backend_chat_completions(
    data: Dict,
    headers: Dict,
    url: str,
    verify: bool = True,
    use_retry: bool = True,
    logger: Optional[logging.Logger] = None,
):
    """
    Отправить запрос в backend chat completions API.

    url передаётся явно вызывающим кодом (как правило, из ctx.config_dict).
    """
    if logger is not None:
        try:
            logger.debug(
                "Send request to backend chat/completions url=%s payload=%s",
                url,
                json.dumps(data, ensure_ascii=False),
            )
        except Exception:
            # Не роняем процесс из-за проблем с сериализацией payload.
            logger.debug("Send request to backend chat/completions url=%s", url)

    r = requests.post(url, json=data, headers=headers, verify=verify)
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        # В случае 422/400 полезно видеть body: там обычно лежит причина валидации.
        err_text = None
        messages_summary = None
        try:
            err_text = r.text
        except Exception:
            err_text = None
        if logger is not None:
            try:
                payload_messages = data.get("messages") if isinstance(data, dict) else None
                if isinstance(payload_messages, list):
                    messages_summary = [
                        {
                            "role": m.get("role") if isinstance(m, dict) else None,
                            "has_content": isinstance(m, dict) and "content" in m,
                            "content_type": type(m.get("content")).__name__
                            if isinstance(m, dict) and "content" in m
                            else None,
                            "has_tool_calls": isinstance(m, dict)
                            and isinstance(m.get("tool_calls"), list),
                            "tool_calls_len": len(m.get("tool_calls"))
                            if isinstance(m, dict) and isinstance(m.get("tool_calls"), list)
                            else None,
                            "has_tool_call_id": isinstance(m, dict)
                            and "tool_call_id" in m,
                        }
                        for m in payload_messages[:20]
                    ]
            except Exception:
                messages_summary = None

            logger.error(
                "Backend chat/completions HTTP error status=%s url=%s response_body=%s",
                getattr(r, "status_code", None),
                url,
                err_text,
            )
            if messages_summary is not None:
                logger.error(
                    "Backend chat/completions request messages summary=%s",
                    messages_summary,
                )
        # Печатаем в stdout, чтобы даже при неполной конфигурации логгера
        # вы точно увидели причину 422 в выводе.
        try:
            # Пробуем подсократить слишком длинные ответы.
            err_text_short = err_text[:4000] if isinstance(err_text, str) else str(err_text)
        except Exception:
            err_text_short = str(err_text)
        print(
            "=== backend chat/completions validation error ===\n"
            f"url={url}\n"
            f"status={getattr(r, 'status_code', None)}\n"
            f"response_body={err_text_short}\n"
        )
        if logger is None and messages_summary is not None:
            print(f"request messages summary={messages_summary}")
        raise

    return r.json()


def create_call_headers(state, ctx) -> Dict[str, str]:
    """
    Сформировать заголовки вызова для backend API на основе State и RunContext.
    """
    config_dict = getattr(ctx, "config_dict", {}) or {}
    auth_connector = getattr(ctx, "auth_connector", None)
    backend_mode = str(config_dict.get("backend_api_mode", "responses")).strip().lower()

    call_headers: Dict[str, str] = {
        "x_cl_id": str(config_dict.get("x_cl_id")),
        "x_trace_id": str(state.state_id),
        "x_rq_id": str(uuid4()),
        "x-session-id": str(state.state_id),
    }
    if state.x_target is not None:
        call_headers["x-target"] = state.x_target

    chat_completions_auth_token = config_dict.get("chat_completions_auth_token")
    use_chat_completions_token = (
        backend_mode == "chat_completions"
        and isinstance(chat_completions_auth_token, str)
        and bool(chat_completions_auth_token.strip())
    )
    if use_chat_completions_token:
        call_headers["Authorization"] = (
            f"Bearer {chat_completions_auth_token.strip()}"
        )
    elif auth_connector is not None:
        token = auth_connector.get_token()
        call_headers["Authorization"] = f"Bearer {token}"

    # OpenRouter рекомендует передавать HTTP-Referer и X-OpenRouter-Title
    # приложения. Эти заголовки опциональны и применяются только для
    # chat/completions режима.
    if backend_mode == "chat_completions":
        openrouter_http_referer = config_dict.get("openrouter_http_referer")
        if isinstance(openrouter_http_referer, str) and openrouter_http_referer.strip():
            call_headers["HTTP-Referer"] = openrouter_http_referer.strip()
        openrouter_x_title = config_dict.get("openrouter_x_title")
        if isinstance(openrouter_x_title, str) and openrouter_x_title.strip():
            title = openrouter_x_title.strip()
            call_headers["X-OpenRouter-Title"] = title
            # Оставляем совместимость со старым алиасом заголовка.
            call_headers["X-Title"] = title
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
    to_send_tools = normalize_chat_completions_tools(to_send_tools)

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
