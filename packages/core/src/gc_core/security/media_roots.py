"""Корни класса `media` защищённых путей.

`media` — файлы, которые агент получает сам, а не пишет в проект: нормализованные
копии изображений (`ImageStore` — корень хранилища и собственный карман процесса)
и картинки/аудио, сохранённые из ответов MCP-тулов. Чтение — без вопроса, запись
тулами — отказ, команды оболочки — с подтверждением (`protected_paths`,
`read_guard`, `command_guard`).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gc_core.attachments.store import ImageStore
from gc_core.config_modules.env_vars import mcp_media_dir


def media_roots(config_dict: Mapping[str, Any]) -> tuple[Path, ...]:
    """Корни `media` для этого конфига: корень хранилища картинок, собственный
    карман процесса (`<корень>/_staging/<pid>`) и каталог MCP-медиа.

    Карманы других процессов лежат под корнем хранилища, но в класс не входят —
    это правило `protected_paths.classify_path`. Корни считаются на каждую проверку
    охраны; каталог тредов не создаётся — только временный корень SDK без каталога
    тредов, приватным, при первом обращении (`attachments.store._private_temp_root`).
    """
    store = ImageStore.for_config(config_dict)
    return (Path(store.root), Path(store.staging_dir), mcp_media_dir())
