"""
Транспортный слой MCP: один запрос — один ответ (синхронно).
Тип транспорта задаётся в конфиге сервера (transport: http | sse | stdio).
"""
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import json
import os
import shutil
import subprocess
import threading
import requests

from gc_core.utils.ssl_config import get_verify_ssl, get_client_cert


def _normalize_url(url: str) -> str:
    if not url.startswith("http://") and not url.startswith("https://"):
        return f"http://{url}"
    return url


def _request_http(
    url: str,
    headers: Dict[str, str],
    json_payload: Dict[str, Any],
    timeout: float,
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
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
        verify=verify,
        cert=cert,
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

    from gc_core.config_modules import env_vars
    debug = env_vars.env.mcp_debug
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
                buffer.append(part)
                in_data = True
                data_str = "\n".join(buffer)
                parsed = _try_parse_json_rpc(data_str)
                if parsed is not None:
                    return parsed
            else:
                # empty "data:" line — reset only if not already accumulating
                if not in_data:
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
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
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
        verify=verify,
        cert=cert,
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


# --------- stdio transport: процесс на один обмен, без долгоживущих процессов ---------

_INIT_PAYLOAD: Dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
        "clientInfo": {"name": "SurfaceClient", "version": "1.0.0"},
    },
}

_INITIALIZED_PAYLOAD: Dict[str, Any] = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}


def _readline_with_timeout(stream: "Any", timeout: float) -> str | None:
    """Прочитать одну строку из stream с таймаутом (кроссплатформенно)."""
    result: list[str | None] = [None]
    def read():
        try:
            line = stream.readline()
            result[0] = line if isinstance(line, str) else (line.decode("utf-8") if line else "")
        except Exception:
            result[0] = None

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return result[0] if result[0] is not None else None


def _resolve_command(command: str) -> str:
    """
    Раскрывает `~`, ищет команду в mcp-env, на Windows резолвит PATHEXT.

    `subprocess.Popen` не делает tilde expansion (это работа шелла), и на
    Windows не учитывает PATHEXT (`npx` не найдёт `npx.cmd`/`.exe`). Делаем
    оба шага здесь — единственный чокпойнт перед спавном stdio MCP.
    """
    from gc_core.mcp_runtime.env import resolve_mcp_command

    return resolve_mcp_command(command)


def _spawn_stdio_process(server_cfg: Dict[str, Any]) -> subprocess.Popen:
    """Запустить процесс MCP-сервера по конфигу (command, args, cwd, env)."""
    command = server_cfg.get("command")
    if not command:
        raise ValueError("stdio MCP server config must have 'command'")
    command = _resolve_command(str(command))
    args = server_cfg.get("args")
    if args is None:
        args = []
    if not isinstance(args, list):
        args = [args]
    cmd = [command] + [str(a) for a in args]
    cwd = server_cfg.get("cwd")
    env_extra = server_cfg.get("env") or {}
    from gc_core.mcp_runtime.env import augment_process_env

    env = augment_process_env()
    for k, v in env_extra.items():
        env[str(k)] = str(v)

    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
        env=env,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )


def _stdio_exchange(
    server_cfg: Dict[str, Any],
    timeout: float,
    steps: List[Tuple[Dict[str, Any], bool]],
) -> List[Dict[str, Any]]:
    """
    Запустить процесс, выполнить последовательность шагов (payload, expect_response),
    после каждого payload при expect_response=True прочитать одну строку JSON-RPC.
    Процесс по завершении обмена завершается.
    """
    proc = _spawn_stdio_process(server_cfg)
    results: List[Dict[str, Any]] = []
    try:
        for payload, expect_response in steps:
            line_out = json.dumps(payload) + "\n"
            proc.stdin.write(line_out)
            proc.stdin.flush()
            if expect_response:
                line_in = _readline_with_timeout(proc.stdout, timeout)
                if line_in is None:
                    results.append({})
                else:
                    line_in = line_in.strip()
                    try:
                        results.append(json.loads(line_in) if line_in else {})
                    except json.JSONDecodeError:
                        results.append({})
            else:
                results.append({})
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass
    return results


def _request_stdio(
    server_cfg: Dict[str, Any],
) -> Callable[..., Tuple[Dict[str, str], Dict[str, Any]]]:
    """
    Вернуть функцию запроса для stdio с той же сигнатурой, что и http/sse:
    (url, headers, json_payload, timeout) -> (response_headers, body_dict).
    Один вызов = один процесс: handshake при необходимости, один ответ, процесс завершается.
    """
    cfg = server_cfg

    def request_fn(
        url: str,
        headers: Dict[str, str],
        json_payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        method = (json_payload or {}).get("method")
        if method == "initialize":
            steps: List[Tuple[Dict[str, Any], bool]] = [(json_payload, True)]
            responses = _stdio_exchange(cfg, timeout, steps)
            body = responses[0] if responses else {}
            return ({"mcp-session-id": "stdio"}, body)
        if method == "notifications/initialized":
            steps = [(_INIT_PAYLOAD, True), (json_payload, False)]
            _stdio_exchange(cfg, timeout, steps)
            return ({}, {})
        # tools/list, tools/call и др.: полный handshake в одном процессе
        steps = [(_INIT_PAYLOAD, True), (_INITIALIZED_PAYLOAD, False), (json_payload, True)]
        responses = _stdio_exchange(cfg, timeout, steps)
        body = responses[2] if len(responses) > 2 else {}
        return ({}, body)

    return request_fn


def get_transport(server_cfg: Dict[str, Any]) -> Callable[..., Tuple[Dict[str, str], Dict[str, Any]]]:
    """
    Вернуть функцию запроса по конфигу сервера.

    server_cfg должен содержать ключ "transport" со значением "http", "sse" или "stdio".

    Возвращаемая функция имеет единую сигнатуру для всех транспортов:
        request(url, headers, json_payload, timeout) -> (response_headers, body_dict)
    """
    transport = (server_cfg or {}).get("transport")

    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)

    if transport == "http":
        def request_http(
            url: str,
            headers: Dict[str, str],
            json_payload: Dict[str, Any],
            timeout: float,
        ) -> Tuple[Dict[str, str], Dict[str, Any]]:
            return _request_http(url, headers, json_payload, timeout, verify=verify, cert=cert)

        return request_http

    if transport == "sse":
        def request_sse(
            url: str,
            headers: Dict[str, str],
            json_payload: Dict[str, Any],
            timeout: float,
        ) -> Tuple[Dict[str, str], Dict[str, Any]]:
            return _request_sse(url, headers, json_payload, timeout, verify=verify, cert=cert)

        return request_sse
    if transport == "stdio":
        return _request_stdio(server_cfg)
    raise ValueError(
        f"Invalid or missing 'transport' in MCP server config. "
        f"Expected 'http', 'sse' or 'stdio', got: {transport!r}"
    )


__all__ = [
    "get_transport",
    "_request_http",
    "_request_sse",
    "_request_stdio",
]
