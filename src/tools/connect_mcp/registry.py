from typing import Any, Dict, Optional

import json
import time

import requests

from config import config_dict as config_data
from tools.connect_mcp.client import _tools_list_request, _get_or_init_session_id, _MCP_SESSIONS


def _build_tools_registry(servers_cfg: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Построить словарь всех тулов со всех MCP-серверов (без кэширования).

    Формат результата:

    {
        "<mcp_key>_<tool_name>": {
            "tool": {...},      # сигнатура для LLM
            "metadata": {...},  # служебная информация клиента
        },
        ...
    }
    """

    all_tools: Dict[str, Dict[str, Any]] = {}

    for server_name, server_cfg in servers_cfg.items():
        if not isinstance(server_cfg, dict):
            continue

        url = server_cfg.get("url")
        token = server_cfg.get("token")

        if not url:
            continue

        # Доп. метаданные сервера для включения в описание тулов
        server_display_name = (
            server_cfg.get("server-name")
            or server_cfg.get("serever-name")  # поддержку опечатки в конфиге оставим
            or server_name
        )
        server_description = server_cfg.get("description") or ""

        try:
            # 1) Берём session_id из кэша или инициализируем заново
            session_id = _get_or_init_session_id(server_name, url, token=token)
            if not session_id:
                continue

            try:
                # 2) Пробуем запросить список tools с текущей сессией
                tools = _tools_list_request(url, session_id=session_id, token=token)
            except requests.HTTPError:
                # Считаем, что сессия могла протухнуть — сбрасываем и пробуем ещё раз
                _MCP_SESSIONS.pop(server_name, None)
                session_id = _get_or_init_session_id(server_name, url, token=token)
                if not session_id:
                    continue
                tools = _tools_list_request(url, session_id=session_id, token=token)

        except Exception:
            # Если какой-то MCP-сервер не отвечает или ломается —
            # просто пропускаем его, остальные используем.
            continue

        # Преобразуем MCP tools в формат function‑tools
        for tool in tools:
            if not isinstance(tool, dict):
                continue

            base_name = tool.get("name")
            if not base_name:
                continue

            combined_name = f"{server_name}_{base_name}"

            base_description = tool.get("description") or ""
            extra_desc = f"\n\nFrom server: {server_display_name}"
            if server_description:
                extra_desc += f"\nServer description: {server_description}"

            full_description = (base_description + extra_desc) if base_description else extra_desc.lstrip()

            parameters_schema = (
                tool.get("inputSchema")
                or tool.get("parameters")
                or {"type": "object", "properties": {}, "additionalProperties": False}
            )

            # "чистая" сигнатура инструмента для LLM
            tool_spec: Dict[str, Any] = {
                "type": "function",
                "name": combined_name,
                "description": full_description,
                "parameters": parameters_schema,
                "strict": True,
            }

            # служебная мета‑информация, не отправляется в LLM
            metadata: Dict[str, Any] = {
                "mcp_server_key": server_name,
                "mcp_tool_name": base_name,
                "server_display_name": server_display_name,
                "server_description": server_description,
                "server_url": url,
            }

            all_tools[combined_name] = {
                "tool": tool_spec,
                "metadata": metadata,
            }

    return all_tools


class ToolsCache:
    """
    Класс для кэширования списка MCP-тулов в памяти процесса.

    - TTL кэша берётся из config.yml:

      mcp-meta:
        tools-cache-ttl: 3600   # в секундах

      Если параметр не задан — по умолчанию 3600 секунд (1 час).

    - Ключ кэша привязан к текущему содержимому config_data['mcp-servers'].
    """

    def __init__(self, cfg: Dict[str, Any], ttl_default: int = 3600) -> None:
        self._config = cfg
        self._ttl_default = ttl_default
        self._data: Optional[Dict[str, Dict[str, Any]]] = None
        self._fetched_at: Optional[float] = None
        self._cache_key: Optional[str] = None

    def _get_ttl(self) -> int:
        mcp_meta = self._config.get("mcp-meta")
        ttl_cfg = mcp_meta.get("tools-cache-ttl") if isinstance(mcp_meta, dict) else None
        try:
            return int(ttl_cfg) if ttl_cfg is not None else self._ttl_default
        except (TypeError, ValueError):
            return self._ttl_default

    def _make_cache_key(self, servers_cfg: Dict[str, Dict[str, Any]]) -> Optional[str]:
        """
        Построить ключ кэша на основе текущей конфигурации MCP-серверов.
        Если сериализация не удалась — возвращаем None и кэш не используем.
        """
        try:
            return json.dumps(servers_cfg, sort_keys=True, default=str)
        except TypeError:
            return None

    def _is_valid(self, cache_key: Optional[str], ttl: int) -> bool:
        if (
            cache_key is None
            or self._data is None
            or self._fetched_at is None
            or self._cache_key != cache_key
        ):
            return False

        return (time.time() - self._fetched_at) < ttl

    def get_tools(self) -> Dict[str, Dict[str, Any]]:
        """
        Получить объединённый словарь тулов с учётом TTL и кэша.
        """
        servers_cfg = self._config.get("mcp-servers") or {}
        ttl = self._get_ttl()
        cache_key = self._make_cache_key(servers_cfg)

        if self._is_valid(cache_key, ttl):
            return self._data or {}

        # Кэш пустой или протух — перестраиваем реестр тулов.
        fresh_tools = _build_tools_registry(servers_cfg)

        if cache_key is not None:
            self._data = fresh_tools
            self._fetched_at = time.time()
            self._cache_key = cache_key

        return fresh_tools

    def force_refresh(self) -> Dict[str, Dict[str, Any]]:
        """
        Принудительно перестроить реестр тулов и обновить кэш,
        игнорируя TTL.
        """
        servers_cfg = self._config.get("mcp-servers") or {}
        cache_key = self._make_cache_key(servers_cfg)

        fresh_tools = _build_tools_registry(servers_cfg)

        if cache_key is not None:
            self._data = fresh_tools
            self._fetched_at = time.time()
            self._cache_key = cache_key

        return fresh_tools


# Глобальный инстанс кэша тулов, инициализируется при старте процесса.
TOOLS_CACHE = ToolsCache(config_data)


def get_all_tools(servers_cfg: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Обёртка для совместимости: получить все тулы через глобальный TOOLS_CACHE.
    Аргумент servers_cfg сейчас игнорируется и берётся из config_data.
    """
    return TOOLS_CACHE.get_tools()


__all__ = [
    "_build_tools_registry",
    "ToolsCache",
    "TOOLS_CACHE",
    "get_all_tools",
]
