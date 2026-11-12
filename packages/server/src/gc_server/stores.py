"""Хранилища процесса из ``storage:`` config.yml: один раз до открытия портов.

Файловые части не открываются здесь: файловое хранилище каждого прогона
строится из настроек его поверхности (у chat и a2a свои специнструкции).
"""

from __future__ import annotations

from typing import Any

from gc_core.stores import Stores


async def aopen_process_stores(settings: Any) -> Stores | None:
    """Связка хранилищ или ``None``, если обе части — файлы."""
    from gc_core.settings import settings_of

    if settings_of(settings).storage.is_files():
        return None
    from gc_stores.configured import aopen_configured

    return await aopen_configured(settings)
