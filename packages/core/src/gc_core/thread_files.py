"""Файлы треда в хранилище тредов: картинки и медиа инструментов.

Хранилище тредов без файлов (база, S3, память SDK) держит их вложениями
треда v2 (``put_blob``/``get_blob``) и удаляет вместе с тредом. Файл треда
агент видит ссылкой ``thread-file://<система>/<тред>/<имя>``: по ней
``fs_view_image`` и запрос к модели читают байты через хранилище
(``blob_ref_of``). Файловое хранилище держит картинки рядом с тредами —
ссылка у него путь на диске.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, unquote

from gc_core.threads.models import BlobRef, ThreadKey

THREAD_FILE_SCHEME = "thread-file://"
#: Тип вложения, когда по ссылке его не узнать: хранилищу для чтения
#: хватает треда и имени.
_OCTET = "application/octet-stream"
#: Имя файла треда: подкаталоги и имя из безопасных символов, без ``..``.
_NAME = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")


def check_name(name: str) -> str:
    if not _NAME.fullmatch(name) or any(
        part in (".", "..") for part in name.split("/")
    ):
        raise ValueError(f"Недопустимое имя файла треда: {name!r}")
    return name


def safe_name(name: str) -> str:
    """Имя файла треда из произвольного (кириллица, пробелы — имя файла от MCP).

    Годное имя — как есть; иначе недопустимые символы заменяются на ``_``, а
    к основе добавляется хеш исходного имени: два разных имени не сольются в
    одно. Расширение сохраняется.
    """
    try:
        return check_name(name)
    except ValueError:
        pass
    import hashlib

    stem, dot, ext = name.rpartition(".")
    if not dot or not re.fullmatch(r"[A-Za-z0-9]{1,8}", ext):
        stem, ext = name, ""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "file"
    digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{cleaned[:80]}-{digest[:8]}" + (f".{ext}" if ext else "")


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


def blob_ref_of(value: Any) -> BlobRef | None:
    """Вложение треда по ссылке ``thread-file://`` (для ``get_blob``) или
    ``None``."""
    parsed = parse_thread_file_uri(value)
    if parsed is None:
        return None
    system_id, thread_id, name = parsed
    return BlobRef(
        key=ThreadKey(system=system_id, thread=thread_id),
        name=name,
        mime=_OCTET,
        location=value,
    )


__all__ = [
    "THREAD_FILE_SCHEME",
    "blob_ref_of",
    "check_name",
    "is_thread_file_uri",
    "parse_thread_file_uri",
    "safe_name",
    "thread_file_uri",
]
