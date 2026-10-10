import base64
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Optional

from gc_core.config_modules.env_vars import mcp_media_dir
from gc_core.tools.connect_mcp.execute_on import resolve_mcp_client
from gc_core.tools.connect_mcp.transport_policy import (
    resolve_effective_transport,
)
from gc_core.tools.connect_mcp.validation import (
    _validate_tool_arguments,
    format_mcp_tool_result,
)
from gc_core.utils.aretry import retry_with_backoff_async

logger = logging.getLogger(__name__)


def _sanitize_dict(value: Dict[Any, Any]) -> Dict[str, Any]:
    ctype = value.get("type")
    strip_data = ctype in ("image", "audio") and isinstance(
        value.get("data"), str
    )
    out: Dict[str, Any] = {}
    for k, v in value.items():
        # JSON keys must be strings
        key = k if isinstance(k, str) else str(k)
        if strip_data and k == "data":
            out[key] = (
                f"<{ctype} data stripped: {len(value['data'])} base64 chars>"
            )
        else:
            out[key] = _json_sanitize(v)
    return out


def _json_sanitize(value: Any) -> Any:
    """
    Convert arbitrary MCP result to JSON-serializable structure.
    - bytes/bytearray -> "binary data"
    - dict/list/tuple -> recursive
    - everything else -> keep as is when possible; fallback to string repr

    Также вырезает base64-полезную нагрузку из MCP image/audio content-блоков:
    такие блоки приходят рядом с text-блоком, в котором уже есть путь/описание,
    а сырой base64 раздувает историю на ~100k+ токенов за один скриншот и
    пробивает контекст backend-модели.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return "binary data"
    if isinstance(value, dict):
        return _sanitize_dict(value)
    if isinstance(value, (list, tuple)):
        return [_json_sanitize(v) for v in value]
    # Try to keep primitive JSON types as-is
    if isinstance(value, (str, int, float, bool)):
        return value
    # Fallback for unknown objects (e.g. exceptions, custom classes)
    return str(value)


_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
}


#: Куда класть медиа из ответа MCP вместо каталога процесса: ставит вызов
#: инструмента, когда картинки треда живут в хранилище тредов
#: (``media_sink_scope``). ``(имя, байты) -> ссылка``.
_MEDIA_SINK: ContextVar[Any] = ContextVar("mcp_media_sink", default=None)


@contextmanager
def media_sink_scope(ctx: Any):
    """На время вызова инструмента: медиа MCP — файлами треда в хранилище
    тредов (``StoreImageStore.write_media``), если оно их держит. Иначе — как
    раньше, в ``<gc_cache>/mcp-media``."""
    from gc_core.thread_paths import thread_address

    store = getattr(ctx, "image_store", None) if ctx is not None else None
    write = getattr(store, "write_media", None)
    address = thread_address(ctx) if callable(write) else None
    if address is None:
        yield
        return
    token = _MEDIA_SINK.set(lambda name, data: write(*address, name, data))
    try:
        yield
    finally:
        _MEDIA_SINK.reset(token)


def _mcp_media_save_dir() -> str:
    """Куда складывать декодированные image/audio: ``mcp_media_dir()``
    (``<gc_cache>/mcp-media``). Создаётся при первой записи.

    Каталог не настраивается: ключа ``mcp-meta.media_save_dir`` в схеме нет
    (``McpMetaConfig`` — ``extra="forbid"``). Тот же каталог охрана считает
    классом ``media`` (``security.media_roots``) — путь берётся из одной функции.
    """
    return str(mcp_media_dir())


def _pick_filename(
    arguments: Dict[str, Any],
    base_tool_name: str,
    mime: str,
) -> str:
    """Имя файла: из arguments.filename (только basename), иначе autogen.
    Всегда форсим расширение по mimeType, чтобы не было `.png` на jpeg и наоборот.
    """
    ext = _MIME_EXT.get((mime or "").lower(), ".bin")
    fname: Optional[str] = None
    if isinstance(arguments, dict):
        raw_name = (
            arguments.get("filename")
            or arguments.get("file")
            or arguments.get("path")
        )
        if isinstance(raw_name, str) and raw_name.strip():
            fname = os.path.basename(raw_name.strip())
    if not fname:
        fname = f"{base_tool_name}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    stem, _ = os.path.splitext(fname)
    return stem + ext


def _save_media_block(
    block: Dict[str, Any],
    arguments: Dict[str, Any],
    base_tool_name: str,
) -> Dict[str, Any]:
    """Декодировать base64 MCP image/audio content и сохранить на диск.

    В случае успеха удаляем поле `data` и добавляем `path`/`size_bytes`.
    В случае ошибки возвращаем блок как есть — дальше `_json_sanitize`
    сам обрежет `data`, чтобы не взорвать контекст.
    """
    data = block.get("data")
    if not isinstance(data, str) or not data:
        return block
    mime = str(block.get("mimeType") or "")
    sink = _MEDIA_SINK.get()
    try:
        fname = _pick_filename(arguments, base_tool_name, mime)
        raw_bytes = base64.b64decode(data, validate=False)
        if sink is not None:
            # Файл треда в хранилище тредов: на диске процесса ничего.
            path = sink(fname, raw_bytes)
        else:
            save_dir = _mcp_media_save_dir()
            os.makedirs(save_dir, exist_ok=True)
            path = os.path.join(save_dir, fname)
            with open(path, "wb") as f:
                f.write(raw_bytes)
    except Exception as e:
        logger.warning("Failed to save MCP media block: %s", e)
        return block

    out: Dict[str, Any] = {k: v for k, v in block.items() if k != "data"}
    out["path"] = path
    out["size_bytes"] = len(raw_bytes)
    return out


def _rewrite_media_blocks(
    raw: Any,
    arguments: Dict[str, Any],
    base_tool_name: str,
) -> Any:
    """Рекурсивно обходит result, декодирует и сохраняет image/audio блоки,
    заменяя base64 `data` на ссылку `path` на хостовой ФС."""
    if isinstance(raw, dict):
        if raw.get("type") in ("image", "audio") and isinstance(
            raw.get("data"), str
        ):
            return _save_media_block(raw, arguments, base_tool_name)
        return {
            k: _rewrite_media_blocks(v, arguments, base_tool_name)
            for k, v in raw.items()
        }
    if isinstance(raw, list):
        return [
            _rewrite_media_blocks(v, arguments, base_tool_name) for v in raw
        ]
    return raw


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
    raw = _rewrite_media_blocks(raw, arguments, base_tool_name)
    formatted = format_mcp_tool_result(raw) if isinstance(raw, dict) else raw
    formatted_sanitized = _json_sanitize(formatted)

    ok = True
    error: Optional[Dict[str, Any]] = None

    # Common MCP pattern: {"content": ..., "isError": bool}
    if (
        isinstance(formatted_sanitized, dict)
        and formatted_sanitized.get("isError") is True
    ):
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
        "error": error,
        "result": formatted_sanitized,
    }
    return json.dumps(envelope, ensure_ascii=False)


@dataclass(frozen=True)
class _McpCallTarget:
    """Всё, что нужно знать о вызове, после валидации и резолва.

    Вынесено из ``call_mcp_tool``, чтобы синхронный и асинхронный входы
    считали это ОДИНАКОВО. Второй копии подготовки быть не должно: именно на
    таком дублировании уже разъезжалось правило выбора транспорта (см.
    ``transport_policy``).
    """

    server_key: str
    server_cfg: Dict[str, Any]
    base_tool_name: str
    arguments_obj: Dict[str, Any]
    url: str
    token: Optional[str]
    transport: Optional[str]
    effective_transport: Optional[str]
    # Клиент выбран ОДИН раз, здесь: выбор — детерминированное решение по
    # конфигу, и неверная пара локус/транспорт обязана падать сразу, а не
    # ретраиться с backoff и логироваться ERROR трижды (комментарий develop).
    client: Any
    use_retry: bool


def _coerce_mcp_arguments(
    full_tool_name: str, arguments: Dict[str, Any] | str
) -> Any:
    # Поддержка случая, когда arguments приходит как JSON-строка
    if not isinstance(arguments, str):
        return arguments or {}
    try:
        return json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Arguments for tool '{full_tool_name}' are not valid JSON: {e}"
        ) from e


def _split_legacy_tool_name(full_tool_name: str) -> tuple[str, str]:
    # Фоллбек на старый формат имени, если метаданных нет
    if "_" not in full_tool_name:
        raise ValueError(
            f"Tool name '{full_tool_name}' does not contain server prefix '<server>_<tool>'."
        )
    server_key, base_tool_name = full_tool_name.split("_", 1)
    return server_key, base_tool_name


def _check_server_cfg(server_key: str, server_cfg: Dict[str, Any]) -> None:
    transport = server_cfg.get("transport")
    stdio_like = transport in ("stdio", "stream_stdio")
    if not stdio_like and not (server_cfg.get("url") or ""):
        raise ValueError(f"MCP server '{server_key}' has no 'url' in config")
    if stdio_like and not server_cfg.get("command"):
        raise ValueError(
            f"MCP server '{server_key}' ({transport}) has no 'command' in config"
        )


def _prepare_mcp_call(
    full_tool_name: str,
    arguments: Dict[str, Any] | str,
    servers_cfg: Dict[str, Dict[str, Any]],
    tools_registry: Dict[str, Dict[str, Any]] | None,
    default_use_retry: bool,
    os_backend: Any = None,
) -> _McpCallTarget:
    """Разобрать имя тула, найти сервер, провалидировать аргументы.

    Бросает ``ValueError`` с человекочитаемым текстом — контракт прежний, эти
    сообщения видит агент.
    """
    if tools_registry is None:
        # Дискавери — это сеть и запуск процессов; синхронный откат здесь
        # уводил её в `run_coro_blocking` и морозил loop вызывающего на весь
        # обход серверов. Реестр обязан приехать готовым: его собирает
        # `acall_mcp_tool` до разбора имени.
        raise ValueError(
            "tools_registry обязателен: дискавери делает вызывающий (`ToolsCache.aget_tools`), а не разбор имени тула"
        )

    if full_tool_name not in tools_registry:
        raise ValueError(f"Unknown tool '{full_tool_name}'")

    arguments_obj = _coerce_mcp_arguments(full_tool_name, arguments)

    # Берём описание тулы и метаданные.
    # Ожидаемый формат: {"tool": {...}, "metadata": {...}}.
    raw_entry = tools_registry[full_tool_name]
    tool_entry = (
        raw_entry.get("tool")
        if isinstance(raw_entry, dict) and "tool" in raw_entry
        else raw_entry
    )
    metadata = (
        raw_entry.get("metadata", {}) if isinstance(raw_entry, dict) else {}
    )

    # Ключ сервера и реальное имя тулы на MCP‑сервере
    server_key = metadata.get("mcp_server_key")
    base_tool_name = metadata.get("mcp_tool_name") or full_tool_name

    if not server_key:
        server_key, base_tool_name = _split_legacy_tool_name(full_tool_name)

    server_cfg = servers_cfg.get(server_key)
    if not isinstance(server_cfg, dict):
        raise ValueError(f"No MCP server config found for key '{server_key}'")

    transport = server_cfg.get("transport")
    url = server_cfg.get("url") or ""
    token = server_cfg.get("token")
    _check_server_cfg(server_key, server_cfg)

    # Схема параметров тулы (JSON Schema)
    tool_schema = tool_entry.get("parameters") or {}

    # Валидация аргументов
    _validate_tool_arguments(full_tool_name, tool_schema, arguments_obj)

    use_retry = bool(server_cfg.get("use_retry", default_use_retry))

    # Правила выбора (persistent_session, депрекейт sse) — в transport_policy,
    # общем с registry: раньше они дублировались и разъезжались.
    effective_transport = resolve_effective_transport(
        server_cfg, server_name=server_key
    )

    client = resolve_mcp_client(server_key, server_cfg, os_backend=os_backend)

    return _McpCallTarget(
        client=client,
        server_key=server_key,
        server_cfg=server_cfg,
        base_tool_name=base_tool_name,
        arguments_obj=arguments_obj,
        url=url,
        token=token,
        transport=transport,
        effective_transport=effective_transport,
        use_retry=use_retry,
    )


async def acall_mcp_tool(
    full_tool_name: str,
    arguments: Dict[str, Any] | str,
    servers_cfg: Dict[str, Dict[str, Any]],
    tools_registry: Dict[str, Dict[str, Any]] | None = None,
    timeout: float = 10.0,
    default_use_retry: bool = False,
    *,
    os_backend: Any = None,
) -> Any:
    """Вызвать MCP-тул, не занимая поток на время ожидания.

    Подготовка и оформление результата — ровно те же, что у синхронного
    ``call_mcp_tool`` (общие ``_prepare_mcp_call`` и ``_wrap_mcp_result``):
    две копии этой логики неизбежно разъехались бы, и синхронный с
    асинхронным вызовы тула стали бы вести себя по-разному.

    Отличается только середина:

    * SDK-транспорты (``stream_sse`` / ``stream_http`` / ``stream_stdio``, и
      сюда же уходит депрекейтнутый ``sse``) исполняются ПРЯМО на loop
      вызывающего — ни одного потока;
    * легаси (свои ``http`` и ``stdio`` на ``requests`` и ``subprocess``)
      блокирующие, прервать их нечем — уходят в поток, который можно бросить
      по дедлайну. Loop при этом свободен: ``requests`` отпускает GIL на
      сокете (замерено — 21 мс разрыва на двухсекундном вызове против 2016 мс
      при прямом вызове в корутине).
    """
    if tools_registry is None:
        # Реестра нет — значит нужна ДИСКАВЕРИ, а это сеть и запуск процессов.
        # Синхронный `get_all_tools` внутри `_prepare_mcp_call` уводил её в
        # `run_coro_blocking`, то есть морозил loop вызывающего на всё время
        # обхода серверов. Асинхронная форма у кэша есть — берём её здесь, до
        # разбора, и дальше `_prepare_mcp_call` получает готовый реестр.
        from gc_core.tools.connect_mcp.registry import ToolsCache

        cache_cfg: Dict[str, Any] = {"mcp-servers": servers_cfg}
        # Бэкенд тому же кэшу ОБЯЗАТЕЛЕН: по его наличию кэш решает, где
        # поднимать сервер. Без него `execute_on: os_backend` считается
        # «нашим», и для stdio дискавери запускает команду сервера на хосте —
        # мимо песочницы, тогда как вызов тула строкой ниже уходит в неё.
        tools_registry = await ToolsCache(
            cache_cfg, os_backend=os_backend
        ).aget_tools()

    target = _prepare_mcp_call(
        full_tool_name,
        arguments,
        servers_cfg,
        tools_registry,
        default_use_retry,
        os_backend=os_backend,
    )

    @retry_with_backoff_async(
        operation_name=(
            f"MCP tool call ({target.server_key}:{target.base_tool_name})"
        ),
        retry_log_level=logging.ERROR,
        no_more_retries_log_level=logging.ERROR,
    )
    async def _ado_mcp_call() -> Any:
        # Диспетчер один — порт клиента (`McpClientPort`), который develop ввёл
        # в PR #88 вместе с `execute_on`. Прежний switch по транспортам здесь и
        # был той второй копией правил выбора, про которую предупреждает
        # докстринг `_McpCallTarget`: разъезжалась она уже дважды.
        #
        # `acall_tool` порт получил вместе с этим вливанием: у develop он был
        # только синхронным, и звать его отсюда значило морозить loop на всё
        # время обмена. SDK-транспорты внутри идут нативно, легаси и persistent
        # stdio — через `run_abandonable`, ровно как дискавери.
        return await target.client.acall_tool(
            target.base_tool_name, target.arguments_obj, timeout=timeout
        )

    from gc_core.tools.connect_mcp.durable_scope import durable_calls_enabled

    data = await _ado_mcp_call(
        use_retry=target.use_retry and not durable_calls_enabled(),
        logger=logger,
    )

    raw = data.get("result", data) if isinstance(data, dict) else data
    return _wrap_mcp_result(
        full_tool_name=full_tool_name,
        server_key=target.server_key,
        base_tool_name=target.base_tool_name,
        arguments=target.arguments_obj,
        raw=raw,
    )


__all__ = [
    "acall_mcp_tool",
]
