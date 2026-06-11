from typing import Any, Dict, Optional

import json
import threading
import time

import requests

import logging

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


def _humanize_discovery_error(exc: BaseException, server_cfg: dict) -> str:
    # Развернуть ExceptionGroup/TaskGroup (anyio в stream_http) → внутренняя ошибка,
    # иначе причина выглядит как «unhandled errors in a TaskGroup».
    depth = 0
    while getattr(exc, "exceptions", None) and depth < 5:
        exc = exc.exceptions[0]
        depth += 1
    if isinstance(exc, FileNotFoundError):
        cmd = server_cfg.get("command") or "?"
        return f"команда '{cmd}' не найдена"
    if isinstance(exc, TimeoutError):
        return "таймаут подключения"
    name = exc.__class__.__name__
    if "Connection" in name or "ConnectError" in name or isinstance(exc, OSError):
        target = server_cfg.get("url") or server_cfg.get("command") or "?"
        return f"нет соединения с {target}"
    msg = str(exc).strip()
    return (msg[:120] + "…") if len(msg) > 121 else (msg or name)


def _normalize_tool(
    server_name: str,
    tool: Dict[str, Any],
    *,
    transport: str,
    url: str,
    server_display_name: str,
    server_description: str,
) -> Optional[Dict[str, Any]]:
    """
    Нормализовать один MCP-тул в формат function-tools.

    Возвращает dict, несущий имя тула (`name`), готовую сигнатуру (`_spec`)
    и служебную мета-информацию (`_metadata`). Сборка финальной записи
    реестра `{"tool", "metadata"}` — в `_registry_entry`.
    """
    if not isinstance(tool, dict):
        return None

    base_name = tool.get("name")
    if not base_name:
        return None

    combined_name = f"mcp_{server_name}_{base_name}"

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

    return {"name": base_name, "_spec": tool_spec, "_metadata": metadata}


def _registry_entry(
    server_name: str,
    *,
    server_cfg: Dict[str, Any],
    tool: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Собрать финальную запись реестра в формате `{"tool", "metadata"}`.

    `_discover_one` отдаёт уже нормализованные tool-dict'ы (несут
    `_spec`/`_metadata`). На случай сырого tool-dict (без `_spec`) —
    нормализуем здесь, чтобы запись всегда была валидной.
    """
    if "_spec" in tool and "_metadata" in tool:
        return {"tool": tool["_spec"], "metadata": tool["_metadata"]}

    cfg = server_cfg if isinstance(server_cfg, dict) else {}
    transport = cfg.get("transport") or ""
    url = "" if transport in ("stdio", "stream_stdio") else (cfg.get("url") or "")
    server_display_name = (
        cfg.get("server-name") or cfg.get("serever-name") or server_name
    )
    server_description = cfg.get("description") or ""
    normalized = _normalize_tool(
        server_name,
        tool,
        transport=transport,
        url=url,
        server_display_name=server_display_name,
        server_description=server_description,
    )
    return {"tool": normalized["_spec"], "metadata": normalized["_metadata"]}


def _discover_one(
    server_name: str,
    server_cfg: Dict[str, Any],
    *,
    timeout: float,
) -> tuple[list, dict]:
    """
    Дискавери тулов одного MCP-сервера (одна попытка, без backoff).

    Возвращает `(tools, status)`, где `tools` — список нормализованных
    tool-dict'ов (каждый несёт `_spec`/`_metadata`), а `status` — структура
    `{"status", "reason", "tool_count", "elapsed_ms"}`. Никогда не бросает.
    """
    t0 = time.monotonic()
    try:
        if not isinstance(server_cfg, dict):
            raise ValueError("server_cfg не dict")

        url = server_cfg.get("url")
        token = server_cfg.get("token")
        transport = server_cfg.get("transport")

        if transport not in ("http", "sse", "stdio", "stream_sse", "stream_http", "stream_stdio"):
            raise ValueError(f"неподдерживаемый transport: {transport!r}")
        if transport in ("stdio", "stream_stdio"):
            if not server_cfg.get("command"):
                raise ValueError("не задан command для stdio")
            url = ""  # stdio не использует url
        else:
            if not url:
                raise ValueError("не задан url")

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

        if effective_transport == "stream_sse":
            tools_raw = list_tools_stream_sse(
                url=url,
                server_cfg=server_cfg,
                token=token,
                timeout=timeout,
            )
        elif effective_transport == "stream_http":
            tools_raw = list_tools_stream_http(
                url=url,
                server_cfg=server_cfg,
                token=token,
                timeout=timeout,
            )
        elif transport == "stream_stdio":
            tools_raw = list_tools_stream_stdio(
                server_cfg=server_cfg,
                timeout=timeout,
            )
        elif is_persistent_stdio(server_cfg):
            session = PersistentStdioRegistry.instance().get_or_create(
                server_name, server_cfg
            )
            resp = session.call("tools/list", {}, timeout)
            result = resp.get("result") if isinstance(resp, dict) else None
            tools_raw = (
                result.get("tools", [])
                if isinstance(result, dict)
                else []
            )
            if not isinstance(tools_raw, list):
                tools_raw = []
        else:
            session_id = _get_or_init_session_id(
                server_name, url, server_cfg, token=token
            )
            if not session_id:
                raise RuntimeError(
                    f"Failed to initialize MCP session for server '{server_name}'"
                )

            try:
                tools_raw = _tools_list_request(
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
                tools_raw = _tools_list_request(
                    url, session_id=session_id, server_cfg=server_cfg, token=token
                )

        tools: list = []
        for tool in tools_raw:
            normalized = _normalize_tool(
                server_name,
                tool,
                transport=transport,
                url=url,
                server_display_name=server_display_name,
                server_description=server_description,
            )
            if normalized is not None:
                tools.append(normalized)

        elapsed = int((time.monotonic() - t0) * 1000)
        return tools, {
            "status": "connected",
            "reason": None,
            "tool_count": len(tools),
            "elapsed_ms": elapsed,
        }
    except Exception as exc:
        reason = _humanize_discovery_error(
            exc, server_cfg if isinstance(server_cfg, dict) else {}
        )
        logger.warning("MCP discovery failed for %s: %s", server_name, exc)
        print(f"[MCP] {server_name}: недоступен — {reason} (пропущен)")
        elapsed = int((time.monotonic() - t0) * 1000)
        return [], {
            "status": "failed",
            "reason": reason,
            "tool_count": 0,
            "elapsed_ms": elapsed,
        }


def _build_tools_registry(
    servers_cfg: Dict[str, Dict[str, Any]],
    *,
    discovery_timeout_sec: float = 8,
    on_progress=None,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    Построить словарь всех тулов со всех MCP-серверов (без кэширования).

    Дискавери выполняется параллельно (daemon-потоки) с общим дедлайном
    `discovery_timeout_sec` — fail-fast, без долгого backoff.

    Возвращает `(all_tools, status_map)`:

    all_tools = {
        "<mcp_key>_<tool_name>": {
            "tool": {...},      # сигнатура для LLM
            "metadata": {...},  # служебная информация клиента
        },
        ...
    }
    status_map = {
        "<server_name>": {"status", "reason", "tool_count", "elapsed_ms"},
        ...
    }
    """
    items = [(n, c) for n, c in servers_cfg.items()]
    all_tools: Dict[str, Dict[str, Any]] = {}
    status_map: Dict[str, Dict[str, Any]] = {}
    if not items:
        return all_tools, status_map

    results: Dict[str, tuple] = {}

    def _worker(name, cfg):
        result = _discover_one(name, cfg, timeout=discovery_timeout_sec)
        results[name] = result
        if on_progress is not None:
            on_progress(name, result[1])

    threads = []
    for name, cfg in items:
        th = threading.Thread(
            target=_worker,
            args=(name, cfg),
            daemon=True,
            name=f"mcp-discover-{name}",
        )
        th.start()
        threads.append((name, th))

    deadline = time.monotonic() + discovery_timeout_sec
    for name, th in threads:
        th.join(timeout=max(0.0, deadline - time.monotonic()))

    for name, cfg in items:
        if name in results:
            tools, status = results[name]
        else:
            tools, status = [], {
                "status": "failed",
                "reason": "таймаут подключения",
                "tool_count": 0,
                "elapsed_ms": int(discovery_timeout_sec * 1000),
            }
            if on_progress is not None:
                on_progress(name, status)
        status_map[name] = status
        for tool in tools:
            base = tool.get("name")
            if not base:
                continue
            all_tools[f"mcp_{name}_{base}"] = _registry_entry(
                name, server_cfg=cfg, tool=tool
            )
    return all_tools, status_map


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
        self._status: Dict[str, Dict[str, Any]] = {}

    def _discovery_timeout(self) -> int:
        meta = self._config.get("mcp-meta")
        v = meta.get("discovery_timeout_sec") if isinstance(meta, dict) else None
        try:
            return int(v) if v else 8
        except (TypeError, ValueError):
            return 8

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._status)

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

    def get_tools(self, *, on_progress=None) -> Dict[str, Dict[str, Any]]:
        """
        Получить объединённый словарь тулов с учётом TTL и кэша.
        """
        servers_cfg = self._config.get("mcp-servers") or {}
        ttl = self._get_ttl()
        cache_key = self._make_cache_key(servers_cfg)

        if self._is_valid(cache_key, ttl):
            return self._data or {}

        # Кэш пустой или протух — перестраиваем реестр тулов.
        fresh_tools, status = _build_tools_registry(
            servers_cfg,
            discovery_timeout_sec=self._discovery_timeout(),
            on_progress=on_progress,
        )
        self._status = status

        if cache_key is not None:
            self._data = fresh_tools
            self._fetched_at = time.time()
            self._cache_key = cache_key

        return fresh_tools

    def force_refresh(self, *, on_progress=None) -> Dict[str, Dict[str, Any]]:
        """
        Принудительно перестроить реестр тулов и обновить кэш,
        игнорируя TTL.
        """
        servers_cfg = self._config.get("mcp-servers") or {}
        cache_key = self._make_cache_key(servers_cfg)

        fresh_tools, status = _build_tools_registry(
            servers_cfg,
            discovery_timeout_sec=self._discovery_timeout(),
            on_progress=on_progress,
        )
        self._status = status

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
    if mcp_meta is not None:
        cfg["mcp-meta"] = mcp_meta

    cache = ToolsCache(cfg)
    return cache.get_tools()


__all__ = [
    "_build_tools_registry",
    "ToolsCache",
    "get_all_tools",
]
