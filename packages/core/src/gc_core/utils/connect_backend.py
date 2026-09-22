import asyncio
import json
import logging
import os
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import httpx

from gc_core.config_modules.backend import DEFAULT_REQUEST_TIMEOUT_SEC
from gc_core.message_policy.chat_completions import prepare_chat_completion_messages_for_api
from gc_core.model_router import ResolvedModel
from gc_core.trace_id import (
    model_trace_header_enabled,
    trace_header_name,
    trace_id_from_config,
)
from gc_core.usage.context import record_usage
from gc_core.utils.aretry import retry_with_backoff_async
from gc_core.utils.async_http import get_async_client
from gc_core.utils.backend_errors import (
    HTTP_STATUS_ERRORS,
    HTTP_TRANSPORT_ERRORS,
    http_status_code,
    is_empty_completion,
)
from gc_core.utils.chat_completions_stream import BackendStreamError, OnDelta
from gc_core.utils.format_messages import format_message_chat_completions_to_send_backend
from gc_core.utils.http_proxy import resolve_proxies
from gc_core.utils.prompt_cache import apply_prompt_cache, prompt_cache_enabled
from gc_core.utils.schema_markers import schema_without_internal_markers
from gc_core.utils.ssl_config import get_backend_client_cert
from gc_core.utils.stream_assembler import StreamAssembler

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
) -> Tuple[bool, Optional[Tuple[str, str]], bool, float, Optional[str], bool]:
    """``(verify, cert, use_retry, timeout_sec, proxy, retry_embedded_choice_errors)`` — эффективные сетевые параметры
    backend-запроса: ``config_dict`` с наложенным ``resolved.backend_overrides`` поверх
    (``use_ssl``/``cert_path``+``key_path``/``use_retry``/``request_timeout_sec``/``proxy``).

    Единая точка маппинга для обеих воронок (``request_chat_completion_text`` и main-трейс
    в ``processing_responses.py``) — вызывающий код сам решает, когда ``resolved`` уместен
    (например, в responses-режиме per-model сетевые квирки не применяются, спека §4, — тогда
    вызывающий передаёт ``resolved=None``). ``resolved=None`` → байт-в-байт из ``config_dict``,
    как до появления ResolvedModel.
    """
    verify_override = backend_override_value(resolved, "use_ssl")
    verify = config_dict.get("use_ssl", True) if verify_override is NO_BACKEND_OVERRIDE else verify_override
    cert = get_backend_client_cert(effective_cert_config(config_dict, resolved))
    use_retry_override = backend_override_value(resolved, "use_retry")
    use_retry = config_dict.get("use_retry", False) if use_retry_override is NO_BACKEND_OVERRIDE else use_retry_override
    timeout_override = backend_override_value(resolved, "request_timeout_sec")
    timeout_sec = (
        float(config_dict["backend_request_timeout_sec"])
        if timeout_override is NO_BACKEND_OVERRIDE
        else float(timeout_override)
    )
    proxy_override = backend_override_value(resolved, "proxy")
    proxy = config_dict.get("proxy") if proxy_override is NO_BACKEND_OVERRIDE else proxy_override
    # Ретрай ошибки, зашитой прокси в HTTP-200, — параметр той же политики повторов,
    # что и use_retry, и тюнится он под КОНКРЕТНЫЙ прокси. Поэтому живёт здесь, а не
    # читается плоско в каждой воронке: иначе `models.<alias>.backend` принимал бы
    # ключ (BackendConfig целиком), а рантайм молча брал глобальный.
    retry_embedded_override = backend_override_value(resolved, "retry_embedded_choice_errors")
    retry_embedded = (
        config_dict.get("retry_embedded_choice_errors", True)
        if retry_embedded_override is NO_BACKEND_OVERRIDE
        else retry_embedded_override
    )
    return verify, cert, use_retry, timeout_sec, proxy, bool(retry_embedded)


def _function_body_of_flat_tool(tool: Dict) -> Dict:
    fn: Dict = {}
    if "name" in tool:
        fn["name"] = tool.get("name")
    if "description" in tool:
        fn["description"] = tool.get("description")
    if "parameters" in tool:
        fn["parameters"] = schema_without_internal_markers(tool.get("parameters"))
    if "strict" in tool:
        fn["strict"] = tool.get("strict")
    return fn


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
            normalized.append({"type": "function", "function": _function_body_of_flat_tool(tool)})
        else:
            normalized.append(schema_without_internal_markers(tool))

    return normalized


def _default_should_retry_backend_exception(exc: Exception) -> bool:
    """
    Ретраим только вероятно транзиентные ошибки:
    - сетевые ошибки/таймауты
    - HTTP 429 и 5xx
    - HTTP 404: LLM-прокси отдаёт его, пока route модели нет после обрыва
      туннеля (транзиентно); честный 404 просто ждёт retry-бюджет.
    - иногда рвётся стрим: ошибки парсинга/валидации
    """
    if isinstance(exc, HTTP_STATUS_ERRORS):
        status_code = http_status_code(exc)
        if status_code is None:
            return False
        return status_code in (404, 429) or status_code >= 500

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
            api_mode,
            url,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception:
        # Не роняем процесс из-за проблем с сериализацией payload.
        logger.debug("Send request to backend %s url=%s", api_mode, url)
    logger.debug(
        "backend_request",
        extra={"event": "backend_request", "api_mode": api_mode, "url": url, "payload": payload},
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
                "content_type": type(m.get("content")).__name__ if isinstance(m, dict) and "content" in m else None,
                "has_tool_calls": isinstance(m, dict) and isinstance(m.get("tool_calls"), list),
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
            status,
            url,
            err_text,
        )
        if messages_summary is not None:
            logger.error("Backend chat/completions request messages summary=%s", messages_summary)
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
            line, buf = buf[:idx], buf[idx + 1 :]
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
            extra={"event": "backend_request", "api_mode": "responses", "url": url, "payload": data},
        )

    chunks: List[str] = []
    async with client.stream("POST", url, json=data, headers=headers, timeout=timeout) as r:
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            # Тело у потокового ответа не прочитано, и исключение улетало с
            # пустым `response.text`. Для 4xx это ровно та строка, в которой
            # бэкенд объясняет причину (неизвестная модель, превышен лимит):
            # в лог попадал голый код, и разбираться было не с чем. Путь
            # chat/completions это уже делал — здесь была асимметрия.
            err_text = await _aread_error_text(r)
            if logger is not None:
                logger.error(
                    "Backend responses HTTP error status=%s url=%s body=%s",
                    r.status_code,
                    url,
                    (err_text or "")[:2000],
                )
            raise
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


async def _astream_chat_completions(
    client, url: str, data: Dict, headers: Dict, *, timeout: float, logger, on_delta: OnDelta
):
    stream_data = dict(data)
    stream_data["stream"] = True
    # OpenAI-совместимые провайдеры отдают usage в стриме только по явному
    # запросу — иначе финального usage-чанка нет и токены основного цикла
    # (он всегда стримится) не учитываются.
    stream_data["stream_options"] = {"include_usage": True}
    _log_backend_request(logger, "chat_completions", url, stream_data)

    async with client.stream("POST", url, json=stream_data, headers=headers, timeout=timeout) as r:
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            # Диагностика тела сохраняется как в блокирующем пути.
            err_text = await _aread_error_text(r)
            if logger is not None:
                logger.error(
                    "Backend chat/completions (stream) HTTP error status=%s url=%s body=%s",
                    getattr(r, "status_code", None),
                    url,
                    err_text,
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
            if assembler.done:
                # `[DONE]` — ответ кончился, и дочитывать тело до закрытия
                # нельзя: закрывает его сервер, а корп-прокси вправе держать
                # соединение сколько угодно. Ход ждал бы read-timeout (600 с
                # по умолчанию), а тот считается поводом повторить ВЕСЬ
                # запрос: показанный ответ стирается и генерируется заново.
                # Синхронный путь до переезда выходил здесь же —
                # `iter_sse_data_lines` делала на маркере `return`.
                break
        assembler.flush()
        assembled = assembler.result()

        if is_empty_completion(assembled):
            raise BackendStreamError(
                "Backend returned empty stream response (no content, tool_calls, finish_reason, or error)",
                status_code=None,
            )
        if isinstance(assembled, dict):
            record_usage(assembled.get("usage"), url)
        _log_backend_response(logger, "chat_completions", assembled)
        return assembled


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
    # `use_retry` забирает из kwargs декоратор ретраев (retry_with_backoff_async) —
    # до тела функции он не доходит; в сигнатуре остаётся как часть интерфейса.
    del use_retry
    proxies = resolve_proxies(config_proxy, os.environ, url)
    client = get_async_client(verify=verify, cert=cert, proxies=proxies)

    if on_delta is not None:
        return await _astream_chat_completions(
            client, url, data, headers, timeout=timeout, logger=logger, on_delta=on_delta
        )

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
            err_text=err_text,
            url=url,
            data=data,
            logger=logger,
        )
        raise

    payload = r.json()
    if isinstance(payload, dict):
        record_usage(payload.get("usage"), url)
    _log_backend_response(logger, "chat_completions", payload)
    return payload


async def aresolve_auth_token(auth_connector, *, force: bool = False) -> str:
    """Токен, не морозя loop.

    ``AuthConnector.get_token()`` делает два последовательных блокирующих
    HTTP-вызова (GET, потом POST, по 10 с таймаута каждый) одноразовым
    синхронным клиентом. Звали его из ``async def`` напрямую — при истечении
    токена (примерно раз в десять минут) и на каждом 401-ретрае, — и loop мог
    замереть до двадцати секунд, останавливая ВСЕХ агентов и TUI на нём. Это
    ровно тот бюджет отзывчивости, который заявлен в docs/async.md.

    Свой ``aget_token`` у коннектора — идём через него нативно. Нет —
    единственный честный вариант для чужого синхронного кода: поток. Заход в
    пул стоит десятки микросекунд против сотен миллисекунд самого запроса к
    модели, поэтому быстрый путь (токен ещё свеж) отдельно не выделяем — он
    того не стоит, а ветвление стоило бы знания о внутренностях коннектора.
    """
    aget = getattr(auth_connector, "aget_token", None)
    if callable(aget):
        return await aget(force=force)

    # Зовём РОВНО так, как звали синхронные вызывающие: без `force` — совсем
    # без аргумента. Чужие коннекторы (и тестовые двойники) объявляют
    # `get_token()` без параметров, и лишний позиционный аргумент их ломает.
    def _get() -> str:
        return auth_connector.get_token(force=True) if force else auth_connector.get_token()

    return await asyncio.to_thread(_get)


async def acreate_call_headers(state, ctx, resolved: Optional[ResolvedModel] = None) -> Dict[str, str]:
    """Заголовки вызова backend. ЕДИНСТВЕННАЯ форма этой операции.

    Отличие от чистой сборки ровно одно и несущее: токен берётся не на loop.
    Всё остальное — работа со словарями, и она вынесена в
    :func:`build_call_headers`, которая ничего не ждёт и потому двойником не
    является: у неё нет своей формы «сходить за токеном».

    Синхронный близнец снят. Он звал ``auth_connector.get_token()`` прямо —
    два последовательных блокирующих HTTP-вызова по 10 с таймаута, — и это
    единственное, чем он отличался. В проде его не звал никто (оставались
    только мёртвые импорты в ``processing_responses`` и ``llm_chat_client``).
    """
    auth_connector = getattr(ctx, "auth_connector", None)
    token = None
    if auth_connector is not None:
        token = await aresolve_auth_token(auth_connector)
    return build_call_headers(state, ctx, resolved, token=token)


def _authorization_header(authorization_override, auth_connector, token: Optional[str]) -> Optional[str]:
    # authorization: None=наследовать эффективный заголовок, ""=явно без Authorization,
    # значение=использовать как есть (уже Bearer-нормализовано).
    if authorization_override == "":
        return None
    if authorization_override:
        return authorization_override
    if auth_connector is None:
        return None
    # Токен добывает ТОЛЬКО `acreate_call_headers`, и вне loop. Здесь его
    # брать нечем: `AuthConnector.get_token()` — два блокирующих HTTP-вызова
    # по 10 с, и снятая синхронная форма морозила loop ровно на них.
    # `token is None` при живом коннекторе — это незаполненный аргумент, а
    # не «явно без авторизации»; молча уйти без Authorization хуже всего:
    # backend ответит 401, и искать причину будут в конфиге.
    if token is None:
        raise ValueError(
            "build_call_headers: ctx.auth_connector задан, а token не передан. "
            "Заголовки с авторизацией собирает acreate_call_headers — она "
            "берёт токен вне loop."
        )
    return f"Bearer {token}"


def _apply_chat_completion_headers(
    call_headers: Dict[str, str], config_dict: Dict, resolved_for_headers, authorization_override
) -> None:
    """Оверлей backend.chat_completion_headers (per-model приоритетнее глобальных)."""
    extra_headers = backend_override_value(resolved_for_headers, "chat_completion_headers")
    headers_from_model = extra_headers is not NO_BACKEND_OVERRIDE
    if not headers_from_model:
        extra_headers = config_dict.get("chat_completion_headers")
    if not isinstance(extra_headers, dict):
        return
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
        if not headers_from_model and authorization_override is not None and k.lower() == "authorization":
            continue
        if raw_val is None:
            continue
        v = str(raw_val).strip()
        if not v:
            continue
        call_headers[k] = v


def build_call_headers(
    state,
    ctx,
    resolved: Optional[ResolvedModel] = None,
    *,
    token: Optional[str] = None,
) -> Dict[str, str]:
    """
    Собрать заголовки вызова для backend API на основе State и RunContext.

    ЧИСТАЯ часть: ни одного ожидания, ни одного похода в сеть. Токен приходит
    готовым — добывает его :func:`acreate_call_headers`, вне loop.

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
    authorization_override = resolved_for_headers.authorization if resolved_for_headers is not None else None
    authorization = _authorization_header(authorization_override, auth_connector, token)
    if authorization is not None:
        call_headers["Authorization"] = authorization

    # Дополнительные заголовки для chat/completions передаются
    # универсально через backend.chat_completion_headers.
    if backend_mode == "chat_completions":
        _apply_chat_completion_headers(call_headers, config_dict, resolved_for_headers, authorization_override)

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
        if isinstance(m, dict) and "call_id" in m and m.get("type") not in ("function_call", "function_call_output"):
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
    call_data["previous_response_id"] = state.previous_response_id if set_previous_response_id else None
    return call_data


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


def _merge_state_messages(state_messages: Any, messages) -> List[Dict]:
    """История state + новые сообщения без дублей (по id либо по payload)."""
    if not (isinstance(state_messages, list) and len(state_messages) > 0):
        return [m for m in messages if isinstance(m, dict)]
    to_send_messages = state_messages
    msgs_ids = {_message_key(m) for m in state_messages if isinstance(m, dict)}
    for m in messages:
        if not isinstance(m, dict):
            continue
        msg_key = _message_key(m)
        if msg_key not in msgs_ids:
            to_send_messages.append(m)
            msgs_ids.add(msg_key)
    return to_send_messages


def _config_value(resolved: Optional[ResolvedModel], cfg: Dict, key: str, default: Any = None) -> Any:
    """Per-model override из ResolvedModel, иначе значение из config_dict."""
    value = backend_override_value(resolved, key)
    if value is NO_BACKEND_OVERRIDE:
        return cfg.get(key, default)
    return value


def _apply_chat_completion_kwargs(data: Dict, extra: Any, *, prompt_cache_on: bool) -> None:
    if not (isinstance(extra, dict) and extra):
        return
    protected = {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "max_tokens",
        "max_completion_tokens",
    }
    if prompt_cache_on:
        protected.add("cache_control")
    for key, value in extra.items():
        if key in protected:
            if key == "cache_control":
                logger.debug(
                    "chat_completion_kwargs.cache_control игнорируется: разметка prompt cache "
                    "включена (backend.claude_prompt_cache)"
                )
            continue
        data[key] = value


def create_call_data_completions(
    messages,
    state: Optional = None,
    tools: Optional[List] = None,
    model: Optional[str] = None,
    *,
    max_tokens: Optional[int] = None,
    config_dict: Optional[Dict] = None,
    resolved: Optional[ResolvedModel] = None,
) -> Dict:
    state_messages = deepcopy(state.messages) if state is not None else []
    state_tools = deepcopy(state.tools) if state is not None else []
    to_send_messages = _merge_state_messages(state_messages, messages)

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

    configured_max_tokens = _safe_positive_int(_config_value(resolved, cfg, "chat_completions_max_tokens"))
    effective_max_tokens = _safe_positive_int(max_tokens) or configured_max_tokens or 8096

    data = {
        "model": effective_model,
        "messages": to_send_messages_formatted,
        "tools": to_send_tools,
        "tool_choice": "auto",
    }
    _use_max_tokens_param = _config_value(resolved, cfg, "use_max_tokens_param", False)
    # Key selection is provider-driven; explicit max_tokens only sets the VALUE.
    if bool(_use_max_tokens_param):
        data["max_tokens"] = effective_max_tokens
    else:
        data["max_completion_tokens"] = effective_max_tokens

    # Prompt cache (спека prompt-cache §4.2): настройка backend.claude_prompt_cache (per-model
    # поверх глобальной) решает сама; не задана — автодетект по id модели. Резолв — до
    # наложения kwargs: пока разметка наша, пользовательское поле cache_control верхнего
    # уровня на провод не идёт (с другим TTL при нашей метке на последнем блоке провайдер
    # claude-моделей отвечает 400); при выключенной разметке это рабочая настройка
    # пользователя и уходит как задана. Для остальных моделей payload не трогаем — байт в
    # байт как раньше.
    _prompt_cache = _config_value(resolved, cfg, "claude_prompt_cache")
    prompt_cache_on = prompt_cache_enabled(_prompt_cache, effective_model)

    _apply_chat_completion_kwargs(
        data, _config_value(resolved, cfg, "chat_completion_kwargs"), prompt_cache_on=prompt_cache_on
    )

    if prompt_cache_on:
        data = apply_prompt_cache(data)

    return data


if __name__ == "__main__":
    ...
