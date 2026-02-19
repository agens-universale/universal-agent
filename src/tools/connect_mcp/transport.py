"""
Транспортный слой MCP: один запрос — один ответ (синхронно).
Тип транспорта задаётся в конфиге сервера (transport: http | sse).
"""
from typing import Any, Callable, Dict, Tuple

import json
import os
import requests


def _normalize_url(url: str) -> str:
    if not url.startswith("http://") and not url.startswith("https://"):
        return f"http://{url}"
    return url


def _request_http(
    url: str,
    headers: Dict[str, str],
    json_payload: Dict[str, Any],
    timeout: float,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """
    Обычный HTTP: один POST, тело — JSON. Ответ — один JSON-объект.
    """
    url = _normalize_url(url)
    resp = requests.post(
        url,
        json=json_payload,
        headers=headers,
        timeout=timeout,
        stream=False,
    )
    resp.raise_for_status()
    # CaseInsensitiveDict -> dict для единообразия
    out_headers = {k.lower(): v for k, v in resp.headers.items()}
    # Пустое тело (например, на notifications/initialized) — не парсить как JSON
    if not resp.content or not resp.content.strip():
        return out_headers, {}
    try:
        return out_headers, resp.json()
    except (ValueError, json.JSONDecodeError):
        return out_headers, {}


def _try_parse_json_rpc(s: str) -> Dict[str, Any] | None:
    """Распарсить JSON; вернуть объект, если это JSON-RPC ответ с result/error, иначе None."""
    if not s or not s.strip():
        return None
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
            return obj
    except json.JSONDecodeError:
        pass
    return None


def _read_sse_first_json(resp: "requests.Response", timeout: float) -> Dict[str, Any]:
    """
    Читать стрим SSE построчно; вернуть первый JSON-RPC ответ (с result или error).
    Поддерживается многострочное событие: после "data:" следующие строки до пустой
    склеиваются. Парсим после каждой строки — сервер может не слать пустую строку после события.
    """
    buffer: list[str] = []
    in_data = False

    debug = os.environ.get("MCP_DEBUG", "").strip().lower() in ("1", "true", "yes")
    for line in resp.iter_lines(decode_unicode=True):
        if line is None:
            continue
        if debug:
            print(f"[MCP_DEBUG SSE] {line[:200]!r}" + ("..." if len(line) > 200 else ""))
        stripped = line.strip()
        if not stripped:
            if in_data and buffer:
                data_str = "\n".join(buffer)
                parsed = _try_parse_json_rpc(data_str)
                if parsed is not None:
                    return parsed
                buffer = []
            in_data = False
            continue

        if stripped.lower().startswith("data:"):
            prefix_len = 5  # len("data:")
            part = stripped[prefix_len:].strip()
            if part:
                buffer = [part]
                in_data = True
                parsed = _try_parse_json_rpc(part)
                if parsed is not None:
                    return parsed
            else:
                buffer = []
                in_data = True
        elif in_data and buffer:
            buffer.append(stripped)
            data_str = "\n".join(buffer)
            parsed = _try_parse_json_rpc(data_str)
            if parsed is not None:
                return parsed

    if in_data and buffer:
        parsed = _try_parse_json_rpc("\n".join(buffer))
        if parsed is not None:
            return parsed
    return {}


def _request_sse(
    url: str,
    headers: Dict[str, str],
    json_payload: Dict[str, Any],
    timeout: float,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """
    SSE: POST с stream=True, Accept: text/event-stream.
    Читаем первое событие data: {...}, парсим JSON, возвращаем и закрываем соединение.
    """
    url = _normalize_url(url)
    headers = dict(headers)
    headers["Accept"] = "application/json, text/event-stream"

    resp = requests.post(
        url,
        json=json_payload,
        headers=headers,
        timeout=timeout,
        stream=True,
    )
    resp.raise_for_status()
    # Ответ SSE часто в UTF-8; без явной кодировки requests может взять Latin-1 и сломать JSON с кириллицей
    if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = "utf-8"
    out_headers = {k.lower(): v for k, v in resp.headers.items()}
    try:
        body = _read_sse_first_json(resp, timeout)
    finally:
        resp.close()
    return out_headers, body


def get_transport(server_cfg: Dict[str, Any]) -> Callable[..., Tuple[Dict[str, str], Dict[str, Any]]]:
    """
    Вернуть функцию запроса по конфигу сервера.

    server_cfg должен содержать ключ "transport" со значением "http" или "sse".
    Для будущего расширения: "stdio".

    Возвращаемая функция имеет сигнатуру:
        request(url, headers, json_payload, timeout) -> (response_headers, body_dict)
    """
    transport = (server_cfg or {}).get("transport")
    if transport == "http":
        return _request_http
    if transport == "sse":
        return _request_sse
    raise ValueError(
        f"Invalid or missing 'transport' in MCP server config. "
        f"Expected 'http' or 'sse', got: {transport!r}"
    )


__all__ = [
    "get_transport",
    "_request_http",
    "_request_sse",
]
