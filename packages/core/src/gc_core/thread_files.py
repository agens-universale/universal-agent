"""Файлы треда в хранилище тредов: картинки и медиа инструментов.

Необязательная возможность хранилища (как ``replace_messages``): два
синхронных метода

    write_thread_file(system_id, thread_id, name, data: bytes) -> None
    read_thread_file(system_id, thread_id, name) -> bytes | None

Удаляет файлы треда само хранилище — в ``reset`` и ``delete_thread``, вместе с
историей. Хранилище, которое этого не умеет, работает как раньше: картинки и
медиа ложатся файлами на диск процесса. Файловое хранилище (локальный режим)
возможность не объявляет — у него картинки и так рядом с тредом.

Файл треда агент видит ссылкой ``thread-file://<система>/<тред>/<имя>``:
по ней ``fs_view_image`` и запрос к модели читают байты через хранилище.
"""

from __future__ import annotations

import re
from typing import Any, Callable, NamedTuple
from urllib.parse import quote, unquote

THREAD_FILE_SCHEME = "thread-file://"
#: Имя файла треда: подкаталоги и имя из безопасных символов, без ``..``.
_NAME = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")


class ThreadFiles(NamedTuple):
    write: Callable[[str, str, str, bytes], None]
    read: Callable[[str, str, str], bytes | None]


def thread_files_capability(store: Any) -> ThreadFiles | None:
    """Методы файлов треда у хранилища или ``None`` (возможности нет)."""
    write = getattr(store, "write_thread_file", None)
    read = getattr(store, "read_thread_file", None)
    if callable(write) and callable(read):
        return ThreadFiles(write, read)
    return None


def check_name(name: str) -> str:
    if not _NAME.fullmatch(name) or any(
        part in (".", "..") for part in name.split("/")
    ):
        raise ValueError(f"Недопустимое имя файла треда: {name!r}")
    return name


def thread_file_uri(system_id: str, thread_id: str, name: str) -> str:
    return (
        f"{THREAD_FILE_SCHEME}{quote(system_id, safe='')}/"
        f"{quote(thread_id, safe='')}/{check_name(name)}"
    )


def parse_thread_file_uri(value: Any) -> tuple[str, str, str] | None:
    """``(система, тред, имя)`` из ссылки ``thread-file://`` или ``None``."""
    if not isinstance(value, str) or not value.startswith(THREAD_FILE_SCHEME):
        return None
    parts = value[len(THREAD_FILE_SCHEME) :].split("/", 2)
    if len(parts) != 3 or not all(parts):
        return None
    system_id, thread_id, name = parts
    try:
        return unquote(system_id), unquote(thread_id), check_name(name)
    except ValueError:
        return None


def is_thread_file_uri(value: Any) -> bool:
    return parse_thread_file_uri(value) is not None


__all__ = [
    "THREAD_FILE_SCHEME",
    "ThreadFiles",
    "check_name",
    "is_thread_file_uri",
    "parse_thread_file_uri",
    "thread_file_uri",
    "thread_files_capability",
]
