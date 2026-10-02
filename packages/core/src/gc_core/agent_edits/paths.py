"""Канонизация путей агента: один корень трекинга, а не периметр из многих.

Ключ ``canonical_path_key`` — общее имя файла для журнала и снапшотов: под
``launch_root`` относительный POSIX, вне его — абсолютный POSIX (файл ВНЕ
проекта, см. ``is_external_key``). ``write_guard.extra_paths`` расширяет
только право ЗАПИСИ (``gc_core.security``), на отслеживание правок он больше
не влияет. Agent-facing protected (operational / instructions) не трекается
вовсе.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional, Sequence, Union

from gc_core.security.path_policy import (
    is_within_root,
    normalize_path_for_policy,
)
from gc_core.security.protected_paths import classify_path, protected_paths

PathLike = Union[str, os.PathLike[str]]


def _os_name() -> str:
    """Имя платформы; отдельная точка подмены для кроссплатформенных тестов."""
    return os.name


def _case_insensitive() -> bool:
    return _os_name() == "nt"


def is_protected_from_tracking(
    path: PathLike,
    launch_root: PathLike,
    *,
    protected_extra: Sequence[str] | None = None,
) -> bool:
    """Agent-facing protected: operational / instructions — не трекаем."""
    ps = protected_paths(
        cwd=str(launch_root), extra=list(protected_extra or ())
    )
    cls = classify_path(path, ps)
    return cls in ("operational", "instructions")


def canonical_path_key(
    path: PathLike,
    launch_root: PathLike,
    *,
    protected_extra: Sequence[str] | None = None,
) -> Optional[str]:
    """Имя файла в журнале и снапшотах — или ``None``, если путь не трекаем.

    Три исхода, и у каждого свой смысл:

    * под ``launch_root`` → относительный POSIX — полное обслуживание;
    * защищён от отслеживания → ``None`` — не трекаем вовсе;
    * всё остальное → абсолютный POSIX — файл ВНЕ проекта: дифф в ленте есть,
      базы нет, на экране его не будет (``is_external_key``).

    Прежде ``None`` отвечал сразу на два вопроса — «вне периметра» и
    «защищён», — и вызывающий их не различал. Теперь он означает ровно одно.
    """
    try:
        resolved = normalize_path_for_policy(path, launch_root)
        root = normalize_path_for_policy(launch_root, launch_root)
    except (ValueError, OSError):
        return None

    if is_protected_from_tracking(
        resolved, launch_root, protected_extra=protected_extra
    ):
        return None

    ci = _case_insensitive()
    if is_within_root(resolved, root, case_insensitive=ci):
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            # case-insensitive match на Windows: relative_to может не сработать
            # при разном регистре — считаем через relpath.
            rel = os.path.relpath(os.fspath(resolved), os.fspath(root))
            return Path(rel).as_posix()

    return resolved.as_posix()


def is_external_key(path_key: str) -> bool:
    """Ключ файла ВНЕ проекта: базы у него нет, на экране он не показывается.

    Форма ключа и есть признак: под ``launch_root`` он относительный, снаружи —
    абсолютный (см. ``canonical_path_key``). Признак ВЫВОДИТСЯ из пути, и этим
    он лучше флага в записи: флаг забывают на новом пути записи — так тул
    скачивания файла целиком миновал журнал, — а форму ключа забыть нельзя.
    """
    return Path(path_key).is_absolute()


# Теги точек повторной обработки Windows, которые ведут в ДРУГОЕ место: junction
# (mount point) и symlink. Прочие теги — например, заглушки облачных файлов —
# ссылками не являются, и путь через них законен.
_LINK_REPARSE_TAGS = frozenset(
    {
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    }
)


def _is_link(node: Path) -> bool:
    """Звено пути — ссылка: symlink везде, на Windows ещё и junction.

    Junction ловится по тегу из ``lstat``, а не ``os.path.isjunction``: той нет
    до Python 3.12, а поддерживаем 3.11 — на нём проверка молча отдавала бы
    ``False``.
    """
    try:
        st = os.lstat(node)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS


def goes_through_link(launch_root: str, rel: str) -> bool:
    """Идёт ли относительный путь проекта через ссылку — в любом звене, включая последнее.

    По ``lstat`` на звено. Нужна всем, кто пишет или удаляет по пути из
    журнала: ключ записан раньше, и каталог пути с тех пор могли заменить
    ссылкой наружу — запись по нему ушла бы за пределы проекта.
    """
    node = Path(launch_root)
    for part in Path(rel).parts:
        node = node / part
        if _is_link(node):
            return True
    return False
