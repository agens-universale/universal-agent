"""Идентичность треда без тяжёлых импортов: проверка идентификаторов, каталог
тредов из конфига и адрес треда прогона.

Только стандартная библиотека: модуль импортирует хранилище картинок
(``attachments/store.py``), а его — охрана путей и тулы; ``thread_store`` же
через persistence и states тянет реестр тулов и security, и оттуда это был бы
цикл. Раскладка файлов треда (data / meta / sidecar / lock) остаётся у
``thread_store``; проверки идентификаторов и каталог тредов доступны и оттуда.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional

_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_SYSTEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or len(thread_id.strip()) == 0:
        raise ValueError("thread_id должен быть непустой строкой")
    normalized = thread_id.strip()
    if not _THREAD_ID_PATTERN.match(normalized):
        raise ValueError(
            "thread_id может содержать только латинские буквы, цифры, '_' и '-'"
        )
    return normalized


def sanitize_system_id(system_id: str) -> str:
    """
    Санитизировать идентификатор внешней системы для использования в имени файла.

    Разрешаем только латинские буквы, цифры, '_' и '-'.
    Остальные символы заменяем на '_'.
    """
    if not isinstance(system_id, str):
        raise ValueError("system_id должен быть строкой")

    s = system_id.strip().lower()
    if not s:
        raise ValueError("system_id должен быть непустой строкой")

    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s)
    s = s.strip("_")
    if not s:
        raise ValueError("system_id после санитизации стал пустым")
    if not _SYSTEM_ID_PATTERN.match(s):
        # На случай логической ошибки при замене символов.
        raise ValueError(
            "system_id может содержать только латинские буквы, цифры, '_' и '-'"
        )
    return s


def resolve_threads_dir(config_dict: Mapping[str, Any]) -> str:
    """
    Определить директорию для хранения тредов.

    Контракт: config_dict["threads_data_path"] должен быть АБСОЛЮТНЫМ
    путём, заполненным config_modules.root.resolve_paths(). Пустое значение
    означает, что resolve_paths() не вызывался — это ошибка интеграции.
    """
    path_cfg = config_dict.get("threads_data_path")
    if not isinstance(path_cfg, str) or len(path_cfg.strip()) == 0:
        raise ValueError(
            "threads_data_path пуст; "
            "config_modules.root.resolve_paths() должен был его заполнить"
        )

    path_cfg = path_cfg.strip()
    if not os.path.isabs(path_cfg):
        raise ValueError(
            f"threads_data_path должен быть абсолютным путём, получено: {path_cfg!r}"
        )
    return path_cfg


def thread_address(ctx: Any) -> Optional[tuple[str, str]]:
    """Адрес треда прогона ``(system_id, thread_id)``; ``None`` — прогон без треда.

    Поля ставит ``tools.subagents.sidecar.bind_thread_identity`` (прогон с тредом;
    ребёнок получает адрес родителя) и снимает ``clear_thread_identity``. Нужен
    тем, кто кладёт файлы в тред: копии картинок сообщений и ответов человека,
    снимки ``fs_view_image``.
    """
    system_id = getattr(ctx, "system_id", None)
    thread_id = getattr(ctx, "thread_id", None)
    if (
        isinstance(system_id, str)
        and system_id.strip()
        and isinstance(thread_id, str)
        and thread_id.strip()
    ):
        return system_id, thread_id
    return None
