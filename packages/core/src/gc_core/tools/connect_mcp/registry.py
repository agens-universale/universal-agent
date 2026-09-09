from typing import Any, Dict, Optional

import asyncio
import json
import time


import logging

logger = logging.getLogger(__name__)
from gc_core.tools.connect_mcp.transport_policy import resolve_effective_transport
from gc_core.tools.connect_mcp.client import _tools_list_request, _get_or_init_session_id, _MCP_SESSIONS
from gc_core.tools.connect_mcp.persistent_stdio import PersistentStdioRegistry, is_persistent_stdio
from gc_core.tools.connect_mcp.stream_client import (
    alist_tools_stream_http,
    alist_tools_stream_sse,
    alist_tools_stream_stdio,
)
from gc_core.tools.connect_mcp.abandonable import run_abandonable
from gc_core.utils.aio_compat import run_coro_blocking
from gc_core.tools.connect_mcp.execute_on import resolve_mcp_client


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


def _apply_tools_whitelist(
    server_name: str, server_cfg: Dict[str, Any], tools_raw: list
) -> list:
    """Отфильтровать tools_raw по mcp.servers.<name>.whitelist (если enabled)."""
    wl = server_cfg.get("whitelist") if isinstance(server_cfg, dict) else None
    if not isinstance(wl, dict) or not wl.get("enabled"):
        return tools_raw
    allowed = {
        str(t).strip() for t in (wl.get("tools") or []) if str(t).strip()
    }
    present = {
        str(t.get("name")) for t in tools_raw if isinstance(t, dict)
    }
    missing = sorted(allowed - present)
    if missing:
        logger.warning(
            "[MCP] %s: whitelist names not provided by server: %s",
            server_name,
            ", ".join(missing),
        )
    filtered = [
        t
        for t in tools_raw
        if isinstance(t, dict) and str(t.get("name")) in allowed
    ]
    if len(filtered) != len(tools_raw):
        logger.info(
            "[MCP] %s: %d of %d tools whitelisted",
            server_name,
            len(filtered),
            len(tools_raw),
        )
    return filtered


async def _afetch_tools_sdk(
    effective_transport: str,
    *,
    url: Optional[str],
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float,
) -> list:
    """Список тулов по SDK-транспорту, нативно, без потока.

    Синхронные имена ``list_tools_stream_*`` — шимы через
    ``run_coro_blocking``; из уже асинхронного контекста они завели бы лишний
    loop в отдельном потоке. Здесь берём асинхронные точки напрямую.
    """
    if effective_transport == "stream_sse":
        return await alist_tools_stream_sse(
            url=url, server_cfg=server_cfg, token=token, timeout=timeout
        )
    if effective_transport == "stream_http":
        return await alist_tools_stream_http(
            url=url, server_cfg=server_cfg, token=token, timeout=timeout
        )
    return await alist_tools_stream_stdio(server_cfg=server_cfg, timeout=timeout)


async def _adiscover_one(
    server_name: str,
    server_cfg: Dict[str, Any],
    *,
    timeout: float,
    os_backend: Any = None,
) -> tuple[list, dict]:
    """Дискавери одного сервера, без потока там, где он не нужен.

    Веток у ``_discover_one`` три, и асинхронна из них ровно одна:

    * SDK (``stream_sse``/``stream_http``/``stream_stdio``) — насквозь async,
      здесь берётся напрямую;
    * persistent stdio — блокирующий ``Popen``, сессия делится между loop'ами
      (см. докстроку ``persistent_stdio``), остаётся в потоке;
    * легаси ``http``/``sse`` — синхронный ``httpx.Client``, тоже в потоке.

    Поэтому двойник ЗАКОРАЧИВАЕТ первую ветку и отдаёт остальное прежнему
    синхронному телу. Дублировать разбор конфига, нормализацию тулов и разбор
    ошибок не нужно — они одинаковы для всех веток.

    Поток здесь — ``run_abandonable``, а не ``asyncio.to_thread``, и это важно:
    у последнего пул дефолтный, а ``asyncio.run`` на выходе дожидается ВСЕХ его
    воркеров, так что зависший сервер держал бы процесс уже после истёкшего
    дедлайна (см. ``abandonable``).
    """
    transport = server_cfg.get("transport") if isinstance(server_cfg, dict) else None
    # Короткое замыкание в SDK — только когда говорим С ХОСТА агента.
    #
    # `_adiscover_one_sdk` ходит из нашего процесса и про `os_backend` не
    # знает. Для сервера с `execute_on: os_backend` это значит «нет
    # соединения» на дискавери, хотя ВЫЗОВЫ тулов потом пойдут через
    # `OsBackendStatelessClient` и прекрасно достучатся: сервер виден только
    # изнутри песочницы. Симптом — сервер молча без тулов.
    #
    # С бэкендом отдаём общей ветке ниже: она резолвит клиента правильно.
    if isinstance(server_cfg, dict) and transport is not None and os_backend is None:
        effective = resolve_effective_transport(server_cfg, server_name=server_name)
        if effective in ("stream_sse", "stream_http", "stream_stdio"):
            return await _adiscover_one_sdk(
                server_name, server_cfg, effective_transport=effective, timeout=timeout
            )
    return await run_abandonable(
        _discover_one, server_name, server_cfg, timeout=timeout, os_backend=os_backend
    )


async def _adiscover_one_sdk(
    server_name: str,
    server_cfg: Dict[str, Any],
    *,
    effective_transport: str,
    timeout: float,
) -> tuple[list, dict]:
    """SDK-ветка ``_discover_one`` без потока. Ошибки оформляет так же."""
    t0 = time.monotonic()
    try:
        tools_raw = await _afetch_tools_sdk(
            effective_transport,
            url=server_cfg.get("url") or "",
            server_cfg=server_cfg,
            token=server_cfg.get("token"),
            timeout=timeout,
        )
        tools_raw = _apply_tools_whitelist(server_name, server_cfg, tools_raw)
        tools: list = []
        for tool in tools_raw:
            normalized = _normalize_tool(
                server_name,
                tool,
                transport=server_cfg.get("transport"),
                url=server_cfg.get("url") or "",
                server_display_name=(
                    server_cfg.get("server-name")
                    or server_cfg.get("serever-name")
                    or server_name
                ),
                server_description=server_cfg.get("description") or "",
            )
            if normalized is not None:
                tools.append(normalized)
        return tools, {
            "status": "connected",
            "reason": None,
            "tool_count": len(tools),
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 — дискавери никогда не бросает наверх
        reason = _humanize_discovery_error(exc, server_cfg)
        logger.warning("MCP discovery failed for %s: %s", server_name, exc)
        print(f"[MCP] {server_name}: недоступен — {reason} (пропущен)")
        return [], {
            "status": "failed",
            "reason": reason,
            "tool_count": 0,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }


def _discover_one(
    server_name: str,
    server_cfg: Dict[str, Any],
    *,
    timeout: float,
    os_backend: Any = None,
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

        client = resolve_mcp_client(server_name, server_cfg, os_backend=os_backend)
        tools_raw = client.list_tools(timeout=timeout)

        tools_raw = _apply_tools_whitelist(server_name, server_cfg, tools_raw)

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


async def _abuild_tools_registry(
    servers_cfg: Dict[str, Dict[str, Any]],
    *,
    discovery_timeout_sec: float = 8,
    on_progress=None,
    os_backend: Any = None,
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

    async def _one(name: str, cfg: Dict[str, Any]) -> None:
        # _adiscover_one сама решает, нужен ли поток: SDK-транспорты она берёт
        # нативно, а легаси и persistent stdio уводит в run_abandonable.
        result = await _adiscover_one(
            name, cfg, timeout=discovery_timeout_sec, os_backend=os_backend
        )
        results[name] = result
        if on_progress is not None:
            on_progress(name, result[1])

    async def _all() -> None:
        # Общий дедлайн на весь веер — как и был. Не успевшие попадают в ветку
        # «таймаут подключения» ниже: их просто нет в results.
        try:
            async with asyncio.timeout(discovery_timeout_sec):
                await asyncio.gather(
                    *(_one(name, cfg) for name, cfg in items),
                    return_exceptions=True,
                )
        except TimeoutError:
            pass

    await _all()

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


def _build_tools_registry(
    servers_cfg: Dict[str, Dict[str, Any]],
    *,
    discovery_timeout_sec: float = 8,
    on_progress=None,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Синхронный двойник. Реализация одна — асинхронная.

    ``run_coro_blocking``, а НЕ портал: портал отказывается работать из-под
    работающего loop (SyncApiInsideEventLoopError), а сюда законно приходят
    вызывающие, у которых loop уже крутится. До переезда дискавери звалась
    через тот же хелпер — семантику сохраняем.
    """
    return run_coro_blocking(
        _abuild_tools_registry(
            servers_cfg,
            discovery_timeout_sec=discovery_timeout_sec,
            on_progress=on_progress,
        )
    )



class ToolsCache:
    """
    Класс для кэширования списка MCP-тулов в памяти процесса.

    - TTL кэша берётся из config.yml:

      mcp-meta:
        tools-cache-ttl: 3600   # в секундах

      Если параметр не задан — по умолчанию 3600 секунд (1 час).

    - Ключ кэша привязан к текущему содержимому config_data['mcp-servers'].
    """

    def __init__(
        self,
        cfg: Dict[str, Any],
        ttl_default: int = 3600,
        *,
        os_backend: Any = None,
    ) -> None:
        """
        cfg — словарь с mcp-конфигурацией, ожидаются ключи:
        - "mcp-servers": {...}
        - "mcp-meta": {...} (опционально, для TTL).
        """
        self._config = cfg
        self._ttl_default = ttl_default
        self._os_backend = os_backend
        self._data: Optional[Dict[str, Dict[str, Any]]] = None
        self._fetched_at: Optional[float] = None
        self._cache_key: Optional[str] = None
        self._status: Dict[str, Dict[str, Any]] = {}

    def rebind(self, cfg: Dict[str, Any]) -> None:
        """Переключить кэш на новый config-словарь, СОХРАНИВ содержимое.

        Нужно, когда кэшем владеет вызывающий (см. ``get_mcp_tools_data_for_config``):
        каждая сборка контекста пересобирает ``cfg`` заново, и без этого кэш
        считал бы TTL и таймаут по словарю, с которым его когда-то создали.

        Данные НЕ сбрасываем: попадание определяется ключом от содержимого
        ``mcp-servers`` (``_make_cache_key``), а не тождеством словаря. Набор
        серверов не поменялся — кэш валиден; поменялся — ``_is_valid`` увидит
        расхождение ключа и сходит в дискавери сам.
        """
        self._config = cfg

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

    async def aget_tools(self, *, on_progress=None) -> Dict[str, Dict[str, Any]]:
        """Объединённый словарь тулов с учётом TTL и кэша, без потока.

        Основная реализация; синхронный ``get_tools`` — шим через портал.
        Попадание в кэш вообще не трогает сеть, поэтому дешёвый путь остаётся
        дешёвым в обеих формах.
        """
        servers_cfg = self._config.get("mcp-servers") or {}
        ttl = self._get_ttl()
        cache_key = self._make_cache_key(servers_cfg)

        if self._is_valid(cache_key, ttl):
            return self._data or {}

        # Кэш пустой или протух — перестраиваем реестр тулов.
        fresh_tools, status = await _abuild_tools_registry(
            servers_cfg,
            discovery_timeout_sec=self._discovery_timeout(),
            on_progress=on_progress,
            os_backend=self._os_backend,
        )
        self._status = status

        if cache_key is not None:
            self._data = fresh_tools
            self._fetched_at = time.time()
            self._cache_key = cache_key

        return fresh_tools

    def get_tools(self, *, on_progress=None) -> Dict[str, Dict[str, Any]]:
        """Синхронный двойник. Реализация одна — асинхронная.

        ``run_coro_blocking``, а не портал: сюда законно приходят
        вызывающие с уже работающим loop, а портал на это отвечает отказом.
        """
        return run_coro_blocking(self.aget_tools(on_progress=on_progress))


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
