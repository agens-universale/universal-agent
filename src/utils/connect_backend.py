import logging
import os
import requests
import re
import json
from copy import deepcopy
from uuid import uuid4
from typing import List, Dict, Optional, Tuple


from config_modules.backend import DEFAULT_REQUEST_TIMEOUT_SEC
from message_policy.chat_completions import prepare_chat_completion_messages_for_api
from model_router import ResolvedModel
from utils.schema_markers import schema_without_internal_markers
from utils.convertations import output_chat_completion_to_response
from utils.format_messages import format_message_chat_completions_to_send_backend
from utils.http_proxy import resolve_proxies
from utils.http_session import NO_NETRC_SESSION as _NO_NETRC_SESSION
from utils.retry import retry_with_backoff
from utils.ssl_config import get_backend_client_cert
from usage.context import record_usage
from utils.chat_completions_stream import iter_sse_data_lines, assemble_streamed_completion, OnDelta, BackendStreamError
from utils.backend_errors import is_empty_completion
from trace_id import (
    trace_id_from_config,
    trace_header_name,
    model_trace_header_enabled,
)


logger = logging.getLogger(__name__)

# Сентинел «оверрайда нет» — отличим от легального override-значения None/"" в
# ResolvedModel.backend_overrides (см. backend_override_value).
NO_BACKEND_OVERRIDE = object()


def backend_override_value(resolved: Optional[ResolvedModel], backend_key: str):
    """Значение поля BackendConfig из ``resolved.backend_overrides``, если задано.

    ``backend_key`` — имя поля BackendConfig (не обязательно совпадает с плоским
    ключом config_dict, см. src/config.py::flatten_app_config, напр.
    ``request_timeout_sec`` -> плоский ``backend_request_timeout_sec``).
    Возвращает ``NO_BACKEND_OVERRIDE``, если оверрайда нет (``resolved`` — None или
    ключ не задан явно на модели) — вызывающий код сам решает выражение дефолта для
    соответствующего плоского ключа config_dict, не теряя строгость (KeyError и т.п.).
    """
    if resolved is None:
        return NO_BACKEND_OVERRIDE
    overrides = resolved.backend_overrides or {}
    if backend_key in overrides:
        return overrides[backend_key]
    return NO_BACKEND_OVERRIDE


def effective_cert_config(config_dict: Dict, resolved: Optional[ResolvedModel]) -> Dict:
    """``config_dict`` с наложенными ``cert_path``/``key_path`` из backend_overrides.

    Отдельная функция, а не ``backend_override_value`` по ключу: ``get_backend_client_cert``
    читает оба ключа разом из одного словаря (см. utils/ssl_config.py)."""
    if resolved is None:
        return config_dict
    overrides = resolved.backend_overrides or {}
    cert_overrides = {k: overrides[k] for k in ("cert_path", "key_path") if k in overrides}
    if not cert_overrides:
        return config_dict
    merged = dict(config_dict)
    merged.update(cert_overrides)
    return merged


def resolve_network_params(
    config_dict: Dict, resolved: Optional[ResolvedModel]
) -> Tuple[bool, Optional[Tuple[str, str]], bool, float, Optional[str]]:
    """``(verify, cert, use_retry, timeout_sec, proxy)`` — эффективные сетевые параметры
    backend-запроса: ``config_dict`` с наложенным ``resolved.backend_overrides`` поверх
    (``use_ssl``/``cert_path``+``key_path``/``use_retry``/``request_timeout_sec``/``proxy``).

    Единая точка маппинга для обеих воронок (``request_chat_completion_text`` и main-трейс
    в ``processing_responses.py``) — вызывающий код сам решает, когда ``resolved`` уместен
    (например, в responses-режиме per-model сетевые квирки не применяются, спека §4, — тогда
    вызывающий передаёт ``resolved=None``). ``resolved=None`` → байт-в-байт из ``config_dict``,
    как до появления ResolvedModel.
    """
    verify_override = backend_override_value(resolved, "use_ssl")
    verify = (
        config_dict.get("use_ssl", True)
        if verify_override is NO_BACKEND_OVERRIDE
        else verify_override
    )
    cert = get_backend_client_cert(effective_cert_config(config_dict, resolved))
    use_retry_override = backend_override_value(resolved, "use_retry")
    use_retry = (
        config_dict.get("use_retry", False)
        if use_retry_override is NO_BACKEND_OVERRIDE
        else use_retry_override
    )
    timeout_override = backend_override_value(resolved, "request_timeout_sec")
    timeout_sec = (
        float(config_dict["backend_request_timeout_sec"])
        if timeout_override is NO_BACKEND_OVERRIDE
        else float(timeout_override)
    )
    proxy_override = backend_override_value(resolved, "proxy")
    proxy = (
        config_dict.get("proxy") if proxy_override is NO_BACKEND_OVERRIDE else proxy_override
    )
    return verify, cert, use_retry, timeout_sec, proxy


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
    cert: Optional[Tuple[str, str]] = None,
    use_retry: bool = True,
    logger: Optional[logging.Logger] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
    config_proxy: Optional[str] = None,
) -> Tuple[Dict, Dict]:
    """
    Отправить запрос в backend responses API.

    url и verify передаются явно вызывающим кодом (как правило, из ctx.config_dict).
    timeout — HTTP-таймаут (connect + idle между чанками стрима), секунды.
    config_proxy — backend.proxy; резолв (config/env/NO_PROXY) на каждый post.
    """
    chunks: List[bytes] = []
    proxies = resolve_proxies(config_proxy, os.environ, url)

    data['stream'] = True

    if logger is not None:
        logger.debug(
            "backend_request",
            extra={"event": "backend_request", "api_mode": "responses", "url": url, "payload": data},
        )

    with _NO_NETRC_SESSION.post(
        url,
        json=data,
        headers=headers,
        stream=True,
        verify=verify,
        cert=cert,
        timeout=timeout,
        proxies=proxies,
    ) as r:
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
        if logger is not None:
            logger.error(
                "backend_responses_json_error",
                extra={"event": "backend_responses_json_error", "last_chunk": last_chunk},
            )
        raise e

    result_type = result.get("type")
    if result_type != "response.completed":
        raise ValueError(f'wrong stream finish: {chunks[-1].decode("utf-8")}')

    response_obj = result["response"]
    if isinstance(response_obj, dict):
        record_usage(response_obj.get("usage"))
    if logger is not None:
        logger.debug(
            "backend_response",
            extra={"event": "backend_response", "api_mode": "responses", "response": response_obj},
        )
    return response_obj, r_headers


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
    cert: Optional[Tuple[str, str]] = None,
    use_retry: bool = True,
    logger: Optional[logging.Logger] = None,
    on_delta: Optional[OnDelta] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
    config_proxy: Optional[str] = None,
):
    """
    Отправить запрос в backend chat completions API.

    url передаётся явно вызывающим кодом (как правило, из ctx.config_dict).
    timeout — HTTP-таймаут (connect + idle между чанками стрима), секунды.
    config_proxy — backend.proxy; резолв (config/env/NO_PROXY) на каждый post.
    """
    proxies = resolve_proxies(config_proxy, os.environ, url)
    # Стриминг задействован ТОЛЬКО когда caller передал on_delta (его ставит
    # лишь TUI через ctx.on_stream_delta). CLI/server/swarm → on_delta=None →
    # всегда non-stream, независимо от cfg["stream"]. Флаг stream — TUI-only.
    if on_delta is not None:
        stream_data = dict(data)
        stream_data["stream"] = True
        # OpenAI-совместимые провайдеры отдают usage в стриме только по явному
        # запросу — иначе финального usage-чанка нет и токены основного цикла
        # (он всегда стримится) не учитываются.
        stream_data["stream_options"] = {"include_usage": True}
        if logger is not None:
            try:
                logger.debug(
                    "Send request to backend chat/completions (stream=True) url=%s payload=%s",
                    url,
                    json.dumps(stream_data, ensure_ascii=False),
                )
            except Exception:
                logger.debug(
                    "Send request to backend chat/completions (stream=True) url=%s", url
                )
            logger.debug(
                "backend_request",
                extra={
                    "event": "backend_request",
                    "api_mode": "chat_completions",
                    "url": url,
                    "payload": stream_data,
                },
            )
        with _NO_NETRC_SESSION.post(
            url,
            json=stream_data,
            headers=headers,
            stream=True,
            verify=verify,
            cert=cert,
            timeout=timeout,
            proxies=proxies,
        ) as r:
            try:
                r.raise_for_status()
            except requests.exceptions.HTTPError:
                # Preserve error-body diagnostics (parity with blocking path).
                err_text = None
                try:
                    err_text = r.text
                except Exception:
                    err_text = None
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

            assembled = assemble_streamed_completion(
                iter_sse_data_lines(r.iter_lines()), on_delta=on_delta
            )
            if is_empty_completion(assembled):
                raise BackendStreamError(
                    "Backend returned empty stream response (no content, tool_calls, "
                    "finish_reason, or error)",
                    status_code=None,
                )
                        # основного цикла терялся — фиксировался только non-stream aux-вызов).
            if isinstance(assembled, dict):
                record_usage(assembled.get("usage"))
            if logger is not None:
                logger.debug(
                    "backend_response",
                    extra={
                        "event": "backend_response",
                        "api_mode": "chat_completions",
                        "response": assembled,
                    },
                )
            return assembled

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
        logger.debug(
            "backend_request",
            extra={"event": "backend_request", "api_mode": "chat_completions", "url": url, "payload": data},
        )

    r = _NO_NETRC_SESSION.post(
        url, json=data, headers=headers, verify=verify, cert=cert, timeout=timeout,
        proxies=proxies,
    )
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

    data = r.json()
    if isinstance(data, dict):
        record_usage(data.get("usage"))
    if logger is not None:
        logger.debug(
            "backend_response",
            extra={"event": "backend_response", "api_mode": "chat_completions", "response": data},
        )
    return data


def create_call_headers(state, ctx, resolved: Optional[ResolvedModel] = None) -> Dict[str, str]:
    """
    Сформировать заголовки вызова для backend API на основе State и RunContext.

    ``resolved`` — комплект ResolvedModel этого вызова (Router, спека §6/§7). Per-model
    ``authorization``/``backend.chat_completion_headers`` действуют только в chat_completions —
    в responses-режиме из ResolvedModel применяется только id модели (спека §4), заголовки
    собираются как сегодня.
    """
    config_dict = getattr(ctx, "config_dict", {}) or {}
    auth_connector = getattr(ctx, "auth_connector", None)
    backend_mode = str(config_dict.get("backend_api_mode", "responses")).strip().lower()
    resolved_for_headers = resolved if backend_mode == "chat_completions" else None

    call_headers: Dict[str, str] = {
        "x_cl_id": str(config_dict.get("x_cl_id")),
        "x_rq_id": str(uuid4()),
        "x-session-id": str(state.state_id),
    }
    if state.x_target is not None:
        call_headers["x-target"] = state.x_target

    # authorization: None=наследовать эффективный заголовок (путь ниже, как раньше),
    # ""=явно без Authorization, значение=использовать как есть (уже Bearer-нормализовано).
    authorization_override = (
        resolved_for_headers.authorization if resolved_for_headers is not None else None
    )
    if authorization_override == "":
        pass
    elif authorization_override:
        call_headers["Authorization"] = authorization_override
    elif auth_connector is not None:
        token = auth_connector.get_token()
        call_headers["Authorization"] = f"Bearer {token}"

    # Дополнительные заголовки для chat/completions передаются
    # универсально через backend.chat_completion_headers.
    if backend_mode == "chat_completions":
        extra_headers = backend_override_value(resolved_for_headers, "chat_completion_headers")
        headers_from_model = extra_headers is not NO_BACKEND_OVERRIDE
        if not headers_from_model:
            extra_headers = config_dict.get("chat_completion_headers")
        if isinstance(extra_headers, dict):
            for key, raw_val in extra_headers.items():
                if not isinstance(key, str):
                    continue
                k = key.strip()
                if not k:
                    continue
                # ОБЩИЙ Authorization не перебивает решение модели: боевой конфиг
                # держит корп-токен в backend.chat_completion_headers, и без этой
                # ветки `token: sk-or-...` уехал бы под корп-заголовком на чужой
                # хост, а `token: ""` («явно без авторизации») воскрес бы. Оверлей
                # заголовков С ТОЙ ЖЕ модели — тоже per-model решение, и более
                # конкретное: он уточняет token и проходит дальше. Сравнение
                # регистронезависимое: иначе `authorization:` строчными не перебивал
                # бы, а ДОБАВЛЯЛСЯ вторым заголовком к нашему.
                if (
                    not headers_from_model
                    and authorization_override is not None
                    and k.lower() == "authorization"
                ):
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
    resolved: Optional[ResolvedModel] = None,
) -> Dict:
    # Responses-режим: из ResolvedModel действует только провайдерский id модели —
    # url/token/backend-квирки per-model в этом API-режиме не применяются (спека §4).
    call_data = dict()
    call_data["model"] = resolved.model if resolved is not None else state.default_model
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
    resolved: Optional[ResolvedModel] = None,
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
    if resolved is not None:
        effective_model = resolved.model

    cfg = config_dict if isinstance(config_dict, dict) else {}

    _max_tokens_override = backend_override_value(resolved, "chat_completions_max_tokens")
    configured_max_tokens = _safe_positive_int(
        cfg.get("chat_completions_max_tokens")
        if _max_tokens_override is NO_BACKEND_OVERRIDE
        else _max_tokens_override
    )
    explicit_max_tokens = _safe_positive_int(max_tokens)
    effective_max_tokens = explicit_max_tokens or configured_max_tokens or 8096

    data = {
        "model": effective_model,
        "messages": to_send_messages_formatted,
        "tools": to_send_tools,
        "tool_choice": "auto",
    }
    _use_max_tokens_param = backend_override_value(resolved, "use_max_tokens_param")
    if _use_max_tokens_param is NO_BACKEND_OVERRIDE:
        _use_max_tokens_param = cfg.get("use_max_tokens_param", False)
    # Key selection is provider-driven; explicit max_tokens only sets the VALUE.
    if bool(_use_max_tokens_param):
        data["max_tokens"] = effective_max_tokens
    else:
        data["max_completion_tokens"] = effective_max_tokens

    extra = backend_override_value(resolved, "chat_completion_kwargs")
    if extra is NO_BACKEND_OVERRIDE:
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
