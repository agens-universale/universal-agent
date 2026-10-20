"""Хранилища gc-cli из ``storage:`` config.yml.

Ядро знает только схему секции; базу открывает пакет gc-stores. Его может не
быть (gc-cli без него ставится): тогда нефайловый ``storage`` — отказ с
подсказкой, а не молчаливая запись в файлы.
"""

from __future__ import annotations

import sys
from typing import Any

from gc_core.config_modules.storage import FILES, PARTS
from gc_core.settings import settings_of
from gc_core.stores import Stores, run_store_kwargs
from gc_core.threads.facade import Threads


async def aopen_cli_stores(settings: Any) -> tuple[Stores | None, int | None]:
    """``(хранилища, None)``; ``(None, None)`` — всё на файлах;
    ``(None, код выхода)`` — отказ уже объяснён в stderr."""
    storage = settings_of(settings).storage
    if storage.is_files():
        return None, None
    try:
        from gc_stores.configured import StorageConfigError, aopen_configured
    except ImportError:
        print(f"[gc] {_missing_package_text(storage)}", file=sys.stderr)
        return None, 1
    try:
        return await aopen_configured(settings), None
    except StorageConfigError as exc:
        print(f"[gc] {exc}", file=sys.stderr)
        return None, 1


def cli_threads(settings: Any, stores: Stores | None) -> Threads:
    """Треды запуска: из базы ``storage.threads`` или из каталога тредов."""
    store = run_store_kwargs(settings, stores).get("thread_store")
    if store is None:
        return Threads.from_settings(settings)
    return Threads(store)


def _missing_package_text(storage: Any) -> str:
    parts = ", ".join(
        f"storage.{name}.type={storage.part(name)[0]}"
        for name in PARTS
        if storage.part(name)[0] != FILES
    )
    return (
        f"{parts} в config.yml: хранилище в базе открывает пакет gc-stores, "
        "а он не установлен — поставьте его с драйвером базы "
        "(pip install 'gc-stores[postgres]', [mysql], [redis], [s3]; "
        "SQLite — без extra)"
    )
