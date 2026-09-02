from typing import Any, Dict, Optional

import base64
import json
import logging
import os
import time
import uuid

from gc_core.config import load_config
from gc_core.tools.connect_mcp.execute_on import resolve_mcp_client
from gc_core.tools.connect_mcp.registry import get_all_tools
from gc_core.tools.connect_mcp.validation import (
    _validate_tool_arguments,
    format_mcp_tool_result,
)
from gc_core.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)


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
        ctype = value.get("type")
        if ctype in ("image", "audio") and isinstance(value.get("data"), str):
            size = len(value["data"])
            out: Dict[str, Any] = {}
            for k, v in value.items():
                key = k if isinstance(k, str) else str(k)
                if k == "data":
                    out[key] = f"<{ctype} data stripped: {size} base64 chars>"
                else:
                    out[key] = _json_sanitize(v)
            return out
        out = {}
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


def _default_mcp_media_dir() -> str:
    """Дефолт: gc_paths()['cache'] / mcp-media (XDG на Linux, ~/.gc/cache на Windows)."""
    from gc_core.config_modules.env_vars import gc_paths

    return str(gc_paths()["cache"] / "mcp-media")


def _mcp_media_save_dir() -> str:
    """Куда складывать декодированные image/audio.

    Приоритет: config `mcp-meta.media_save_dir` → gc_paths()['cache']/mcp-media.
    Создаётся при первой записи.
    """
    config = load_config()
    raw = config.get("mcp-meta", {}).get("media_save_dir")
    if raw:
        return os.path.expanduser(str(raw))
    return _default_mcp_media_dir()


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
            arguments.get("filename") or arguments.get("file") or arguments.get("path")
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
    save_dir = _mcp_media_save_dir()
    try:
        os.makedirs(save_dir, exist_ok=True)
        fname = _pick_filename(arguments, base_tool_name, mime)
        path = os.path.join(save_dir, fname)
        raw_bytes = base64.b64decode(data, validate=False)
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
        if raw.get("type") in ("image", "audio") and isinstance(raw.get("data"), str):
            return _save_media_block(raw, arguments, base_tool_name)
        return {
            k: _rewrite_media_blocks(v, arguments, base_tool_name)
            for k, v in raw.items()
        }
    if isinstance(raw, list):
        return [_rewrite_media_blocks(v, arguments, base_tool_name) for v in raw]
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


def call_mcp_tool(
    full_tool_name: str,
    arguments: Dict[str, Any] | str,
    servers_cfg: Dict[str, Dict[str, Any]],
    tools_registry: Dict[str, Dict[str, Any]] | None = None,
    timeout: float = 10.0,
    default_use_retry: bool = False,
    *,
    os_backend: Any = None,
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
    tool_entry = (
        raw_entry.get("tool")
        if isinstance(raw_entry, dict) and "tool" in raw_entry
        else raw_entry
    )
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
    stdio_like = transport in ("stdio", "stream_stdio")
    if not stdio_like and not url:
        raise ValueError(f"MCP server '{server_key}' has no 'url' in config")
    if stdio_like and not server_cfg.get("command"):
        raise ValueError(
            f"MCP server '{server_key}' ({transport}) has no 'command' in config"
        )

    # Схема параметров тулы (JSON Schema)
    tool_schema = tool_entry.get("parameters") or {}

    # Валидация аргументов
    _validate_tool_arguments(full_tool_name, tool_schema, arguments_obj)

    use_retry = bool(server_cfg.get("use_retry", default_use_retry))

    # Resolved once, outside the retry: picking the client is a deterministic
    # config decision, and a bad locus/transport pair must fail immediately
    # instead of being retried with backoff and logged as ERROR three times.
    client = resolve_mcp_client(server_key, server_cfg, os_backend=os_backend)

    @retry_with_backoff(
        operation_name=f"MCP tool call ({server_key}:{base_tool_name})",
        retry_log_level=logging.ERROR,
        no_more_retries_log_level=logging.ERROR,
    )
    def _do_mcp_call() -> Any:
        return client.call_tool(base_tool_name, arguments_obj, timeout=timeout)

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
