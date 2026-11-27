"""Каталог тулов MCP на диске: сырой ответ ``tools/list`` по определению.

Следующий запуск видит тулы сервера сразу, не дожидаясь подключения.
Файл — ``<gc_cache>/mcp/catalog/<хеш ключа>/<отпечаток каталога>.json``:
ключ сервера в имя не попадает (в нём бывают любые символы), а отпечаток
каталога секретов не содержит, поэтому смена токена находит прежний файл.
Определение — словарь ``plain_dump(model, by_alias=True)`` без trace.

Чтение не бросает: битый файл, чужая версия формата и любая ошибка
доступа — как отсутствие каталога. Запись тоже не бросает: каталог без
права записи подключение не срывает.
"""

import contextlib
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from gc_core.config_modules.env_vars import gc_paths
from gc_core.tools.connect_mcp.server_identity import catalog_fingerprint
from gc_core.utils.atomic_io import atomic_replace

logger = logging.getLogger(__name__)

CATALOG_FORMAT = 1
#: Сколько самых свежих файлов держать на ключ сервера.
CATALOG_KEEP = 5


def _key_dir(server_key: str) -> Path:
    digest = hashlib.sha256(server_key.encode("utf-8")).hexdigest()[:16]
    return gc_paths()["cache"] / "mcp" / "catalog" / digest


def _catalog_path(server_key: str, server_cfg: dict) -> Path:
    name = f"{catalog_fingerprint(server_cfg)}.json"
    return _key_dir(server_key) / name


def read_catalog(server_key: str, server_cfg: dict) -> list[dict] | None:
    """Сырой список тулов для этого определения; нет каталога — ``None``."""
    try:
        path = _catalog_path(server_key, server_cfg)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != CATALOG_FORMAT:
        return None
    tools = data.get("tools")
    return tools if isinstance(tools, list) else None


def write_catalog(
    server_key: str, server_cfg: dict, tools: list[dict]
) -> None:
    """Сохранить сырой список тулов; лишние старые файлы ключа убрать.

    Ошибка записи — строка в лог, без исключения.
    """
    try:
        path = _catalog_path(server_key, server_cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, {"format": CATALOG_FORMAT, "tools": tools})
        _evict_stale(path.parent)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(
            "[MCP] каталог тулов %s не записан: %s", server_key, exc
        )


def _write_atomic(path: Path, payload: Any) -> None:
    """Временный файл в том же каталоге и замена: читатель не видит
    недописанного, параллельные писатели — чужого временного файла."""
    fd, staged = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        atomic_replace(staged, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staged)
        raise


def _evict_stale(key_dir: Path) -> None:
    """Оставить ``CATALOG_KEEP`` самых свежих по ``(mtime, имя)`` файлов.

    Файл, который убрал параллельный писатель, пропускается.
    """
    ranked: list[tuple[int, str]] = []
    for entry in key_dir.glob("*.json"):
        with contextlib.suppress(FileNotFoundError):
            ranked.append((entry.stat().st_mtime_ns, entry.name))
    ranked.sort(reverse=True)
    for _, name in ranked[CATALOG_KEEP:]:
        (key_dir / name).unlink(missing_ok=True)


__all__ = [
    "CATALOG_FORMAT",
    "CATALOG_KEEP",
    "read_catalog",
    "write_catalog",
]
