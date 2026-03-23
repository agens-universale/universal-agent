from typing import Any, Dict, Optional

import json
import logging
import requests

from tools.connect_mcp.client import _MCP_SESSIONS, _call_mcp_tool, _get_or_init_session_id
from tools.connect_mcp.registry import get_all_tools
from tools.connect_mcp.stream_client import (
    call_tool_stream_http,
    call_tool_stream_sse,
    call_tool_stream_stdio,
)
from tools.connect_mcp.validation import _validate_tool_arguments, format_mcp_tool_result
from utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)


def _json_sanitize(value: Any) -> Any:
    """
    Convert arbitrary MCP result to JSON-serializable structure.
    - bytes/bytearray -> "binary data"
    - dict/list/tuple -> recursive
    - everything else -> keep as is when possible; fallback to string repr
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return "binary data"
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            # JSON keys must be strings
            key = k if isinstance(k, str) else str(k)
            out[key] = _json_sanitize(v)
        return out
    if isinstance(value, list):
        return [_json_sanitize(v) for v in value]
    if isinstance(value, tuple):
        return [_json_sanitize(v) for v in value]
    # Try to keep primitive JSON types as-is
    if isinstance(value, (str, int, float, bool)):
        return value
    # Fallback for unknown objects (e.g. exceptions, custom classes)
    return str(value)


def _wrap_mcp_result(
    *,
    full_tool_name: str,
    server_key: Optional[str],
    base_tool_name: str,
    arguments: Dict[str, Any],
    raw: Any,
) -> str:
    """
    Normalize ANY MCP tool output into a stable JSON envelope.
    """
    formatted = format_mcp_tool_result(raw) if isinstance(raw, dict) else raw
    formatted_sanitized = _json_sanitize(formatted)

    ok = True
    error: Optional[Dict[str, Any]] = None

    # Common MCP pattern: {"content": ..., "isError": bool}
    if isinstance(formatted_sanitized, dict) and formatted_sanitized.get("isError") is True:
        ok = False
        error = {
            "type": "mcp_tool_error",
            "message": "MCP tool returned isError=true",
        }

    envelope = {
        "tool": full_tool_name,
        "ok": ok,
        "mcp_server": server_key,
        "mcp_tool": base_tool_name,
        "arguments": _json_sanitize(arguments),
        "result": formatted_sanitized,
        "error": error,
    }
    return json.dumps(envelope, ensure_ascii=False)


def call_mcp_tool(
    full_tool_name: str,
    arguments: Dict[str, Any] | str,
    servers_cfg: Dict[str, Dict[str, Any]],
    tools_registry: Dict[str, Dict[str, Any]] | None = None,
    timeout: float = 10.0,
    default_use_retry: bool = False,
) -> Any:
    """
    Вызвать инструмент на соответствующем MCP-сервере.

    full_tool_name: имя вида "<mcp_key>_<tool_name>" (как в get_all_tools).
    arguments: словарь аргументов ИЛИ JSON-строка с аргументами (как в OpenAI tool_call).

    tools_registry:
        результат get_all_tools(servers_cfg) — словарь всех инструментов.
        Если None — будет получен заново.

    Поведение:
    - валидирует arguments против JSON Schema из tools_registry[full_tool_name]['parameters'];
      при ошибке бросает ValueError с подробным описанием путей и сообщений;
    - если всё валидно — вызывает соответствующий MCP tool через tools/call
      и возвращает result (data['result'] из MCP-ответа).
    """
    if tools_registry is None:
        tools_registry = get_all_tools(servers_cfg)

    if full_tool_name not in tools_registry:
        raise ValueError(f"Unknown tool '{full_tool_name}'")

    # Поддержка случая, когда arguments приходит как JSON-строка
    if isinstance(arguments, str):
        try:
            arguments_obj = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Arguments for tool '{full_tool_name}' are not valid JSON: {e}"
            ) from e
    else:
        arguments_obj = arguments or {}

    # Берём описание тулы и метаданные.
    # Ожидаемый формат: {"tool": {...}, "metadata": {...}}.
    raw_entry = tools_registry[full_tool_name]
    tool_entry = raw_entry.get("tool") if isinstance(raw_entry, dict) and "tool" in raw_entry else raw_entry
    metadata = raw_entry.get("metadata", {}) if isinstance(raw_entry, dict) else {}

    # Ключ сервера и реальное имя тулы на MCP‑сервере
    server_key = metadata.get("mcp_server_key")
    base_tool_name = metadata.get("mcp_tool_name") or full_tool_name

    if not server_key:
        # Фоллбек на старый формат имени, если метаданных нет
        if "_" not in full_tool_name:
            raise ValueError(
                f"Tool name '{full_tool_name}' does not contain server prefix '<server>_<tool>'."
            )
        server_key, base_tool_name = full_tool_name.split("_", 1)

    server_cfg = servers_cfg.get(server_key)
    if not isinstance(server_cfg, dict):
        raise ValueError(f"No MCP server config found for key '{server_key}'")

    transport = server_cfg.get("transport")
    url = server_cfg.get("url") or ""
    token = server_cfg.get("token")
    if transport != "stdio" and not url:
        raise ValueError(f"MCP server '{server_key}' has no 'url' in config")
    if transport == "stdio" and not server_cfg.get("command"):
        raise ValueError(f"MCP server '{server_key}' (stdio) has no 'command' in config")

    # Схема параметров тулы (JSON Schema)
    tool_schema = tool_entry.get("parameters") or {}

    # Валидация аргументов
    _validate_tool_arguments(full_tool_name, tool_schema, arguments_obj)

    use_retry = bool(server_cfg.get("use_retry", default_use_retry))

    @retry_with_backoff(
        operation_name=f"MCP tool call ({server_key}:{base_tool_name})",
        retry_log_level=logging.ERROR,
        no_more_retries_log_level=logging.ERROR,
    )
    def _do_mcp_call() -> Any:
        if transport == "stream_sse":
            return call_tool_stream_sse(
                url=url,
                server_cfg=server_cfg,
                token=token,
                timeout=timeout,
                tool_name=base_tool_name,
                arguments=arguments_obj,
            )
        if transport == "stream_http":
            return call_tool_stream_http(
                url=url,
                server_cfg=server_cfg,
                token=token,
                timeout=timeout,
                tool_name=base_tool_name,
                arguments=arguments_obj,
            )
        if transport == "stream_stdio":
            return call_tool_stream_stdio(
                server_cfg=server_cfg,
                timeout=timeout,
                tool_name=base_tool_name,
                arguments=arguments_obj,
            )

        session_id = _get_or_init_session_id(
            server_key, url, server_cfg, token=token, timeout=timeout
        )
        if not session_id:
            raise RuntimeError(f"Failed to initialize MCP session for server '{server_key}'")

        def do_call(sid: str):
            return _call_mcp_tool(
                url=url,
                session_id=sid,
                tool_name=base_tool_name,
                arguments=arguments_obj,
                server_cfg=server_cfg,
                token=token,
                timeout=timeout,
                server_key=server_key,
            )

        try:
            data_local = do_call(session_id)
        except requests.HTTPError:
            _MCP_SESSIONS.pop(server_key, None)
            session_id = _get_or_init_session_id(
                server_key, url, server_cfg, token=token, timeout=timeout
            )
            if not session_id:
                raise RuntimeError(
                    f"Failed to re-initialize MCP session for server '{server_key}'"
                )
            data_local = do_call(session_id)

        if data_local == {} and transport == "sse":
            _MCP_SESSIONS.pop(server_key, None)
            session_id = _get_or_init_session_id(
                server_key, url, server_cfg, token=token, timeout=timeout
            )
            if session_id:
                data_local = do_call(session_id)
        return data_local

    data = _do_mcp_call(
        use_retry=use_retry,
        logger=logger,
    )

    if not isinstance(data, dict):
        # Unknown/legacy transport returned non-dict; still normalize
        return _wrap_mcp_result(
            full_tool_name=full_tool_name,
            server_key=server_key,
            base_tool_name=base_tool_name,
            arguments=arguments_obj,
            raw=data,
        )

    result = data.get("result", data)
    return _wrap_mcp_result(
        full_tool_name=full_tool_name,
        server_key=server_key,
        base_tool_name=base_tool_name,
        arguments=arguments_obj,
        raw=result,
    )


__all__ = [
    "call_mcp_tool",
]
