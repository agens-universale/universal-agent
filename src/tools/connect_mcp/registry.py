from typing import Any, Dict, Optional

import json
import time
import traceback

import requests

import logging
from utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)
from tools.connect_mcp.client import _tools_list_request, _get_or_init_session_id, _MCP_SESSIONS
from tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
    is_persistent_stdio,
)
from tools.connect_mcp.stream_client import (
    list_tools_stream_http,
    list_tools_stream_sse,
    list_tools_stream_stdio,
)


def _build_tools_registry(
    servers_cfg: Dict[str, Dict[str, Any]],
    *,
    default_use_retry: bool = False,
) -> Dict[str, Dict[str, Any]]:
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
        transport = server_cfg.get("transport")
        use_retry = bool(server_cfg.get("use_retry", default_use_retry))

        if transport not in ("http", "sse", "stdio", "stream_sse", "stream_http", "stream_stdio"):
            continue
        if transport in ("stdio", "stream_stdio"):
            if not server_cfg.get("command"):
                continue
            url = ""  # stdio не использует url
        else:
            if not url:
                continue

        # Доп. метаданные сервера для включения в описание тулов
        server_display_name = (
            server_cfg.get("server-name")
            or server_cfg.get("serever-name")  # поддержку опечатки в конфиге оставим
            or server_name
        )
        server_description = server_cfg.get("description") or ""

        # persistent_session унифицирован: легаси `sse`/`http` транспорт не имеет
        # своей персистентной сессии, поэтому при флаге true роутим через
        # SDK-клиент `stream_sse`/`stream_http` (то же делаем и в call_mcp_tool).
        effective_transport = transport
        if server_cfg.get("persistent_session"):
            if transport == "sse":
                effective_transport = "stream_sse"
            elif transport == "http":
                effective_transport = "stream_http"

        try:
            @retry_with_backoff(
                operation_name=f"MCP tools/list ({server_name})",
                retry_log_level=logging.ERROR,
                no_more_retries_log_level=logging.ERROR,
            )
            def _fetch_tools() -> list[dict]:
                if effective_transport == "stream_sse":
                    return list_tools_stream_sse(
                        url=url,
                        server_cfg=server_cfg,
                        token=token,
                        timeout=10.0,
                    )
                if effective_transport == "stream_http":
                    return list_tools_stream_http(
                        url=url,
                        server_cfg=server_cfg,
                        token=token,
                        timeout=10.0,
                    )
                if transport == "stream_stdio":
                    return list_tools_stream_stdio(
                        server_cfg=server_cfg,
                        timeout=10.0,
                    )
                if is_persistent_stdio(server_cfg):
                    session = PersistentStdioRegistry.instance().get_or_create(
                        server_name, server_cfg
                    )
                    resp = session.call("tools/list", {}, 10.0)
                    result = resp.get("result") if isinstance(resp, dict) else None
                    tools = (
                        result.get("tools", [])
                        if isinstance(result, dict)
                        else []
                    )
                    return tools if isinstance(tools, list) else []

                session_id = _get_or_init_session_id(
                    server_name, url, server_cfg, token=token
                )
                if not session_id:
                    raise RuntimeError(
                        f"Failed to initialize MCP session for server '{server_name}'"
                    )

                try:
                    return _tools_list_request(
                        url, session_id=session_id, server_cfg=server_cfg, token=token
                    )
                except requests.HTTPError:
                    _MCP_SESSIONS.pop(server_name, None)
                    session_id = _get_or_init_session_id(
                        server_name, url, server_cfg, token=token
                    )
                    if not session_id:
                        raise RuntimeError(
                            f"Failed to re-initialize MCP session for server '{server_name}'"
                        )
                    return _tools_list_request(
                        url, session_id=session_id, server_cfg=server_cfg, token=token
                    )

            tools = _fetch_tools(
                use_retry=use_retry,
                logger=logger,
            )

        except Exception as e:
            # Если какой-то MCP-сервер не отвечает или ломается —
            # просто пропускаем его, остальные используем.
            logger.exception(f'{server_name} connection error')
            print(f'[ERROR] {server_name} connection error: {e}')
            print(traceback.format_exc())
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
            server_url = url if transport != "stdio" else ""
            metadata: Dict[str, Any] = {
                "mcp_server_key": server_name,
                "mcp_tool_name": base_name,
                "server_display_name": server_display_name,
                "server_description": server_description,
                "server_url": server_url,
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
        """
        cfg — словарь с mcp-конфигурацией, ожидаются ключи:
        - "mcp-servers": {...}
        - "mcp-meta": {...} (опционально, для TTL).
        """
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
        default_use_retry = bool(self._config.get("use_retry", False))
        ttl = self._get_ttl()
        cache_key = self._make_cache_key(servers_cfg)

        if self._is_valid(cache_key, ttl):
            return self._data or {}

        # Кэш пустой или протух — перестраиваем реестр тулов.
        fresh_tools = _build_tools_registry(
            servers_cfg,
            default_use_retry=default_use_retry,
        )

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
        default_use_retry = bool(self._config.get("use_retry", False))
        cache_key = self._make_cache_key(servers_cfg)

        fresh_tools = _build_tools_registry(
            servers_cfg,
            default_use_retry=default_use_retry,
        )

        if cache_key is not None:
            self._data = fresh_tools
            self._fetched_at = time.time()
            self._cache_key = cache_key

        return fresh_tools


def get_all_tools(
    servers_cfg: Dict[str, Dict[str, Any]],
    mcp_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Получить все MCP‑тулы для заданной конфигурации серверов.

    Функция-обёртка: внутри создаётся временный ToolsCache на основе
    переданных servers_cfg и mcp_meta и возвращается результат get_tools().

    Для более тонкого управления кэшем (например, переиспользование
    инстанса ToolsCache между вызовами) рекомендуется создавать и хранить
    ToolsCache снаружи (например, в run_context).
    """
    cfg: Dict[str, Any] = {"mcp-servers": servers_cfg}
    cfg["use_retry"] = False
    if mcp_meta is not None:
        cfg["mcp-meta"] = mcp_meta

    cache = ToolsCache(cfg)
    return cache.get_tools()


__all__ = [
    "_build_tools_registry",
    "ToolsCache",
    "get_all_tools",
]
