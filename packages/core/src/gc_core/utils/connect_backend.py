import functools
import logging
import os
import httpx
import re
import json
from copy import deepcopy
from uuid import uuid4
from typing import Any, List, Dict, Optional, Tuple


from gc_core.config_modules.backend import DEFAULT_REQUEST_TIMEOUT_SEC
from gc_core.message_policy.chat_completions import prepare_chat_completion_messages_for_api
from gc_core.utils.schema_markers import schema_without_internal_markers
from gc_core.utils.format_messages import format_message_chat_completions_to_send_backend
from gc_core.utils.http_proxy import resolve_proxies
from gc_core.utils.http_session import NO_NETRC_SESSION as _NO_NETRC_SESSION
from gc_core.utils.aretry import retry_with_backoff_async
from gc_core.utils.async_http import get_async_client
from gc_core.utils.portal import get_portal
from gc_core.utils.retry import retry_with_backoff
from gc_core.utils.stream_assembler import StreamAssembler
from gc_core.usage.context import record_usage
from gc_core.utils.chat_completions_stream import iter_sse_data_lines, assemble_streamed_completion, OnDelta, BackendStreamError
from gc_core.utils.backend_errors import HTTP_STATUS_ERRORS, HTTP_TRANSPORT_ERRORS, http_status_code, is_empty_completion
from gc_core.trace_id import (
    trace_id_from_config,
    trace_header_name,
    model_trace_header_enabled,
)


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
                fn["parameters"] = schema_without_internal_markers(tool.get("parameters"))
            if "strict" in tool:
                fn["strict"] = tool.get("strict")

            normalized.append({"type": "function", "function": fn})
            continue

        normalized.append(schema_without_internal_markers(tool))

    return normalized


def _default_should_retry_backend_exception(exc: Exception) -> bool:
    """
    Ретраим только вероятно транзиентные ошибки:
    - сетевые ошибки/таймауты
    - HTTP 429 и 5xx
    - иногда рвётся стрим: ошибки парсинга/валидации
    """
    if isinstance(exc, HTTP_STATUS_ERRORS):
        status_code = http_status_code(exc)
        if status_code is None:
            return False
        return status_code == 429 or status_code >= 500

    if isinstance(exc, HTTP_TRANSPORT_ERRORS):
        return True

    return isinstance(exc, (json.JSONDecodeError, ValueError))



def _log_backend_request(logger, api_mode: str, url: str, payload: Dict) -> None:
    """Лог исходящего запроса. Один на оба транспорта: расхождение в логах
    между sync и async путями искало бы себя часами."""
    if logger is None:
        return
    try:
        logger.debug(
            "Send request to backend %s url=%s payload=%s",
            api_mode, url, json.dumps(payload, ensure_ascii=False),
        )
    except Exception:
        # Не роняем процесс из-за проблем с сериализацией payload.
        logger.debug("Send request to backend %s url=%s", api_mode, url)
    logger.debug(
        "backend_request",
        extra={"event": "backend_request", "api_mode": api_mode,
               "url": url, "payload": payload},
    )


def _log_backend_response(logger, api_mode: str, response: Any) -> None:
    if logger is None:
        return
    logger.debug(
        "backend_response",
        extra={"event": "backend_response", "api_mode": api_mode, "response": response},
    )


def _messages_summary(data: Dict) -> Optional[List[Dict]]:
    """Скелет отправленных сообщений — по нему видно причину 422 без тела запроса."""
    try:
        payload_messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(payload_messages, list):
            return None
        return [
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
                "has_tool_call_id": isinstance(m, dict) and "tool_call_id" in m,
            }
            for m in payload_messages[:20]
        ]
    except Exception:
        return None


def _report_chat_http_error(*, status, err_text, url, data, logger) -> None:
    """Диагностика HTTP-ошибки chat/completions.

    Общая для обоих транспортов. В случае 422/400 причина валидации лежит в
    теле ответа, и печать в stdout здесь намеренная: при неполной конфигурации
    логгера иначе не видно вообще ничего.
    """
    messages_summary = _messages_summary(data) if logger is not None else None
    if logger is not None:
        logger.error(
            "Backend chat/completions HTTP error status=%s url=%s response_body=%s",
            status, url, err_text,
        )
        if messages_summary is not None:
            logger.error(
                "Backend chat/completions request messages summary=%s", messages_summary
            )
    try:
        err_text_short = err_text[:4000] if isinstance(err_text, str) else str(err_text)
    except Exception:
        err_text_short = str(err_text)
    print(
        "=== backend chat/completions validation error ===\n"
        f"url={url}\n"
        f"status={status}\n"
        f"response_body={err_text_short}\n"
    )
    if logger is None and messages_summary is not None:
        print(f"request messages summary={messages_summary}")


async def _aiter_utf8_lines(response: "httpx.Response"):
    """Строки SSE, декодированные строго как UTF-8.

    ``httpx.aiter_lines()`` декодирует по charset из заголовка ответа, а
    синхронный путь до переезда делал ``line.decode("utf-8", errors="replace")``
    независимо от того, что объявил провайдер. Разница не теоретическая:
    провайдер, объявивший ISO-8859-1 или windows-1251, портил ответ там, где
    старый путь давал верный текст. Тело SSE у OpenAI-совместимых API — всегда
    UTF-8, что бы ни стояло в заголовке.
    """
    buf = b""
    async for chunk in response.aiter_bytes():
        buf += chunk
        while True:
            idx = buf.find(b"\n")
            if idx == -1:
                break
            line, buf = buf[:idx], buf[idx + 1:]
            yield line.decode("utf-8", errors="replace")
    if buf:
        yield buf.decode("utf-8", errors="replace")


async def _aread_error_text(response: "httpx.Response") -> Optional[str]:
    """Тело ответа-ошибки. У стримового ответа оно недоступно до aread()."""
    try:
        await response.aread()
        return response.text
    except Exception:
        return None


@retry_with_backoff_async(
    should_retry=_default_should_retry_backend_exception,
    retry_log_level=logging.ERROR,
    no_more_retries_log_level=logging.ERROR,
    operation_name="Backend request",
)
async def arequest_backend_responses(
    data: Dict,
    headers: Dict,
    url: str,
    verify: bool = True,
    cert: Optional[Tuple[str, str]] = None,
    use_retry: bool = True,
    logger: Optional[logging.Logger] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
    config_proxy: Optional[str] = None,
) -> Tuple[Dict, Dict]:
    """Запрос в backend responses API (deprecated-режим), неблокирующий.

    url и verify передаются явно вызывающим кодом (как правило, из ctx.config_dict).
    timeout — HTTP-таймаут (connect + idle между чанками стрима), секунды.
    config_proxy — backend.proxy; резолв (config/env/NO_PROXY) на каждый запрос.
    """
    proxies = resolve_proxies(config_proxy, os.environ, url)
    client = get_async_client(verify=verify, cert=cert, proxies=proxies)

    data["stream"] = True
    if logger is not None:
        logger.debug(
            "backend_request",
            extra={"event": "backend_request", "api_mode": "responses",
                   "url": url, "payload": data},
        )

    chunks: List[str] = []
    async with client.stream(
        "POST", url, json=data, headers=headers, timeout=timeout
    ) as r:
        r.raise_for_status()
        r_headers = r.headers
        async for line in _aiter_utf8_lines(r):
            # Пустые строки — разделители событий SSE. requests.iter_lines их
            # проглатывал, httpx.aiter_lines отдаёт: без фильтра «последним
            # чанком» оказался бы пустой разделитель, и режим responses падал
            # бы на разборе JSON.
            if line.strip():
                chunks.append(line)

    last_chunk = chunks[-1] if chunks else ""
    try:
        result = json.loads(re.sub("^data: +", "", last_chunk))
    except Exception as e:
        print(f"Error read json from backend. Last chunk: {last_chunk}")
        if logger is not None:
            logger.error(
                "backend_responses_json_error",
                extra={"event": "backend_responses_json_error", "last_chunk": last_chunk},
            )
        raise e

    if result.get("type") != "response.completed":
        raise ValueError(f"wrong stream finish: {last_chunk}")

    response_obj = result["response"]
    if isinstance(response_obj, dict):
        record_usage(response_obj.get("usage"), url)
    _log_backend_response(logger, "responses", response_obj)
    return response_obj, r_headers


@functools.wraps(arequest_backend_responses)
def request_backend_responses(*args: Any, **kwargs: Any) -> Tuple[Dict, Dict]:
    """Синхронный двойник. Реализация одна — асинхронная; здесь только портал."""
    return get_portal().run(arequest_backend_responses(*args, **kwargs))


@retry_with_backoff_async(
    should_retry=_default_should_retry_backend_exception,
    retry_log_level=logging.ERROR,
    no_more_retries_log_level=logging.ERROR,
    operation_name="Backend chat completion request",
)
async def arequest_backend_chat_completions(
    data: Dict,
    headers: Dict,
    url: str,
    verify: bool = True,
    cert: Optional[Tuple[str, str]] = None,
    use_retry: bool = True,
    logger: Optional[logging.Logger] = None,
    on_delta: Optional[OnDelta] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
    config_proxy: Optional[str] = None,
):
    """Запрос в backend chat/completions, неблокирующий.

    Стриминг задействован ТОЛЬКО когда caller передал on_delta (его ставит лишь
    TUI через ctx.on_stream_delta). CLI/server/swarm → on_delta=None → всегда
    non-stream, независимо от cfg["stream"]. Флаг stream — TUI-only.
    """
    proxies = resolve_proxies(config_proxy, os.environ, url)
    client = get_async_client(verify=verify, cert=cert, proxies=proxies)

    if on_delta is not None:
        stream_data = dict(data)
        stream_data["stream"] = True
        # OpenAI-совместимые провайдеры отдают usage в стриме только по явному
        # запросу — иначе финального usage-чанка нет и токены основного цикла
        # (он всегда стримится) не учитываются.
        stream_data["stream_options"] = {"include_usage": True}
        _log_backend_request(logger, "chat_completions", url, stream_data)

        async with client.stream(
            "POST", url, json=stream_data, headers=headers, timeout=timeout
        ) as r:
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError:
                # Диагностика тела сохраняется как в блокирующем пути.
                err_text = await _aread_error_text(r)
                if logger is not None:
                    logger.error(
                        "Backend chat/completions (stream) HTTP error status=%s url=%s body=%s",
                        getattr(r, "status_code", None), url, err_text,
                    )
                print(
                    "=== backend chat/completions (stream) error ===\n"
                    f"url={url}\nstatus={getattr(r, 'status_code', None)}\n"
                    f"response_body={(err_text or '')[:4000]}\n"
                )
                raise
            on_delta("reset", "")

            assembler = StreamAssembler(on_delta=on_delta)
            async for line in _aiter_utf8_lines(r):
                assembler.feed_line(line)
            assembler.flush()
            assembled = assembler.result()

            if is_empty_completion(assembled):
                raise BackendStreamError(
                    "Backend returned empty stream response (no content, tool_calls, "
                    "finish_reason, or error)",
                    status_code=None,
                )
            if isinstance(assembled, dict):
                record_usage(assembled.get("usage"), url)
            _log_backend_response(logger, "chat_completions", assembled)
            return assembled

    _log_backend_request(logger, "chat_completions", url, data)
    r = await client.post(url, json=data, headers=headers, timeout=timeout)
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError:
        err_text = None
        try:
            err_text = r.text
        except Exception:
            err_text = None
        _report_chat_http_error(
            status=getattr(r, "status_code", None),
            err_text=err_text, url=url, data=data, logger=logger,
        )
        raise

    payload = r.json()
    if isinstance(payload, dict):
        record_usage(payload.get("usage"), url)
    _log_backend_response(logger, "chat_completions", payload)
    return payload


@functools.wraps(arequest_backend_chat_completions)
def request_backend_chat_completions(*args: Any, **kwargs: Any):
    """Синхронный двойник. Реализация одна — асинхронная; здесь только портал.

    Контракт не изменился: та же сигнатура, тот же результат, блокирующий вызов.
    """
    return get_portal().run(arequest_backend_chat_completions(*args, **kwargs))


def create_call_headers(state, ctx) -> Dict[str, str]:
    """
    Сформировать заголовки вызова для backend API на основе State и RunContext.
    """
    config_dict = getattr(ctx, "config_dict", {}) or {}
    auth_connector = getattr(ctx, "auth_connector", None)
    backend_mode = str(config_dict.get("backend_api_mode", "responses")).strip().lower()

    call_headers: Dict[str, str] = {
        "x_cl_id": str(config_dict.get("x_cl_id")),
        "x_rq_id": str(uuid4()),
        "x-session-id": str(state.state_id),
    }
    if state.x_target is not None:
        call_headers["x-target"] = state.x_target

    if auth_connector is not None:
        token = auth_connector.get_token()
        call_headers["Authorization"] = f"Bearer {token}"

    # Дополнительные заголовки для chat/completions передаются
    # универсально через backend.chat_completion_headers.
    if backend_mode == "chat_completions":
        extra_headers = config_dict.get("chat_completion_headers")
        if isinstance(extra_headers, dict):
            for key, raw_val in extra_headers.items():
                if not isinstance(key, str):
                    continue
                k = key.strip()
                if not k:
                    continue
                if raw_val is None:
                    continue
                v = str(raw_val).strip()
                if not v:
                    continue
                call_headers[k] = v

    # Внешний trace_id — строго opt-in через agent.trace_header_enabled: без
    # флага заголовок не уходит, даже если трейс у рана есть (в лог-файл и в
    # записи логов он при этом попадает как обычно).
    # Инъекция ПОСЛЕ оверлея chat_completion_headers — осознанный приоритет:
    # per-run значение выигрывает у статического YAML с тем же именем. И вне
    # ветки backend_mode: заголовок нужен в обоих режимах API. Дисциплина пустых
    # значений — как у оверлея выше, а не как у x_cl_id, где отсутствие ключа
    # отправляет литерал "None".
    trace_id = trace_id_from_config(config_dict)
    if trace_id and model_trace_header_enabled(config_dict):
        call_headers[trace_header_name(config_dict, "x_trace_id")] = trace_id
    return call_headers


def _responses_input_without_message_call_id(messages):
    """call_id на role-based message-элементах (напр. technical-feedback user) —
    только для TUI/persist; в responses input не отправляем. На function_call/
    function_call_output call_id обязателен — сохраняем. Не мутируем исходники."""
    cleaned = []
    for m in messages:
        if (isinstance(m, dict) and "call_id" in m
                and m.get("type") not in ("function_call", "function_call_output")):
            m = {k: v for k, v in m.items() if k != "call_id"}
        cleaned.append(m)
    return cleaned


def create_call_data_responses(
    messages,
    state,
    tools: Optional[List] = None,
    set_previous_response_id: bool = True,
) -> Dict:
    call_data = dict()
    call_data["model"] = state.default_model
    call_data["input"] = _responses_input_without_message_call_id(messages)
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
    max_tokens: Optional[int] = None,
    config_dict: Optional[Dict] = None,
    **kwargs,
) -> Dict:
    def _safe_positive_int(raw: object) -> Optional[int]:
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return value if value >= 1 else None

    def _message_key(message: Dict) -> str:
        msg_id = message.get("id")
        if isinstance(msg_id, str) and msg_id:
            return f"id:{msg_id}"
        return "payload:" + json.dumps(message, ensure_ascii=False, sort_keys=True)

    if state is not None:
        state_messages = deepcopy(state.messages)
        state_tools = deepcopy(state.tools)
    else:
        state_messages = []
        state_tools = []

    if isinstance(state_messages, list) and len(state_messages) > 0:
        to_send_messages = state_messages
        msgs_ids = set((_message_key(m) for m in state_messages if isinstance(m, dict)))
        for m in messages:
            if not isinstance(m, dict):
                continue
            msg_key = _message_key(m)
            if msg_key not in msgs_ids:
                to_send_messages.append(m)
                msgs_ids.add(msg_key)
    else:
        to_send_messages = [m for m in messages if isinstance(m, dict)]

    to_send_tools = tools if tools is not None else state_tools
    to_send_tools = normalize_chat_completions_tools(to_send_tools)

    to_send_messages_formatted = []
    for m in to_send_messages:
        formatted = format_message_chat_completions_to_send_backend(m)
        if formatted is not None:
            to_send_messages_formatted.append(formatted)
    to_send_messages_formatted = prepare_chat_completion_messages_for_api(
        to_send_messages_formatted,
        keep_latest_unanswered=True,
    )
    effective_model = model
    if effective_model is None and state is not None:
        effective_model = state.default_model

    cfg = config_dict if isinstance(config_dict, dict) else {}
    explicit_max_tokens = _safe_positive_int(max_tokens)
    configured_max_tokens = _safe_positive_int(cfg.get("chat_completions_max_tokens"))
    effective_max_tokens = explicit_max_tokens or configured_max_tokens or 8096

    data = {
        "model": effective_model,
        "messages": to_send_messages_formatted,
        "tools": to_send_tools,
        "tool_choice": "auto",
    }
    # Key selection is provider-driven; explicit max_tokens only sets the VALUE.
    if bool(cfg.get("use_max_tokens_param", False)):
        data["max_tokens"] = effective_max_tokens
    else:
        data["max_completion_tokens"] = effective_max_tokens

    extra = cfg.get("chat_completion_kwargs")
    if isinstance(extra, dict) and extra:
        protected = {
            "model",
            "messages",
            "tools",
            "tool_choice",
            "max_tokens",
            "max_completion_tokens",
        }
        for key, value in extra.items():
            if key in protected:
                continue
            data[key] = value

    return data
    
if __name__ == "__main__":
    ...
