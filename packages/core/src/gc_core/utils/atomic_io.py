"""Атомарная замена файла, устойчивая к транзиентным Windows-локам.

``os.replace`` на Windows (``MoveFileEx`` c ``REPLACE_EXISTING``) отдаёт
``ACCESS_DENIED (5)`` или ``SHARING_VIOLATION (32)``, когда на целевой или
исходный файл в этот момент открыт чужой хэндл — конкурентный reader в том
же процессе (например, TUI-поток читает meta, пока run-поток его перезаписывает),
антивирус или Search-индексатор. На POSIX ``rename`` в этой ситуации не падает.

Ретраим ТОЛЬКО транзиентные Windows-коды ограниченное число раз; любую другую
ошибку (реальный permission-error, ENOENT, и весь POSIX, где ``winerror`` нет)
пробрасываем сразу — семантика ``os.replace`` сохраняется, атомарность не трогаем.
"""

import contextlib
import os
import time
import uuid

_TRANSIENT_WINERRORS = (5, 32)


def atomic_replace(
    src, dst, *, attempts: int = 5, base_delay: float = 0.01
) -> None:
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            transient = getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS
            if not transient or attempt >= attempts:
                raise
            time.sleep(base_delay * attempt)


def write_text_atomic(path: str, text: str) -> None:
    """Записать файл целиком через свой временный файл и ``os.replace``.

    Временный файл уникален на каждую запись (рядом с целью, тот же диск):
    параллельные писатели одного файла не делят его, читатель видит либо
    старый, либо новый файл целиком. Права — обычные (umask), как у
    ``open``, а не 0600 у ``mkstemp``.
    """
    tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "x", encoding="utf-8") as f:
            f.write(text)
        atomic_replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
