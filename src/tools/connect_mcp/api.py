from typing import Any, Dict

import json
import requests

from config import config_dict as config_data
from tools.connect_mcp.client import _call_mcp_tool, _get_or_init_session_id
from tools.connect_mcp.registry import get_all_tools
from tools.connect_mcp.validation import _validate_tool_arguments, format_mcp_tool_result


def call_mcp_tool(
    full_tool_name: str,
    arguments: Dict[str, Any] | str,
    servers_cfg: Dict[str, Dict[str, Any]] | None = None,
    tools_registry: Dict[str, Dict[str, Any]] | None = None,
    timeout: float = 10.0,
) -> Any:
    """
    Вызвать инструмент на соответствующем MCP-сервере.

    full_tool_name: имя вида "<mcp_key>_<tool_name>" (как в get_all_tools).
    arguments: словарь аргументов ИЛИ JSON-строка с аргументами (как в OpenAI tool_call).

    servers_cfg:
        как правило, config_data['mcp-servers'].
        Если None — берётся из config_data.

    tools_registry:
        результат get_all_tools(servers_cfg) — словарь всех инструментов.
        Если None — будет получен заново.

    Поведение:
    - валидирует arguments против JSON Schema из tools_registry[full_tool_name]['parameters'];
      при ошибке бросает ValueError с подробным описанием путей и сообщений;
    - если всё валидно — вызывает соответствующий MCP tool через tools/call
      и возвращает result (data['result'] из MCP-ответа).
    """
    if servers_cfg is None:
        servers_cfg = config_data.get("mcp-servers") or {}

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

    url = server_cfg.get("url")
    token = server_cfg.get("token")
    if not url:
        raise ValueError(f"MCP server '{server_key}' has no 'url' in config")

    # Схема параметров тулы (JSON Schema)
    tool_schema = tool_entry.get("parameters") or {}

    # Валидация аргументов
    _validate_tool_arguments(full_tool_name, tool_schema, arguments_obj)

    # MCP-сессия
    session_id = _get_or_init_session_id(server_key, url, token=token, timeout=timeout)
    if not session_id:
        raise RuntimeError(f"Failed to initialize MCP session for server '{server_key}'")

    try:
        data = _call_mcp_tool(
            url=url,
            session_id=session_id,
            tool_name=base_tool_name,
            arguments=arguments_obj,
            token=token,
            timeout=timeout,
        )
    except requests.HTTPError:
        # Пытаемся переинициализировать сессию один раз, как в get_all_tools
        from connect_mcp.client import _MCP_SESSIONS

        _MCP_SESSIONS.pop(server_key, None)
        session_id = _get_or_init_session_id(server_key, url, token=token, timeout=timeout)
        if not session_id:
            raise RuntimeError(
                f"Failed to re-initialize MCP session for server '{server_key}'"
            )
        data = _call_mcp_tool(
            url=url,
            session_id=session_id,
            tool_name=base_tool_name,
            arguments=arguments_obj,
            token=token,
            timeout=timeout,
        )

    if not isinstance(data, dict):
        return data

    result = data.get("result", data)
    return format_mcp_tool_result(result)


__all__ = [
    "call_mcp_tool",
]
