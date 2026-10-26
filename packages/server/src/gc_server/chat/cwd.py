"""Каталог хода из ``metadata.cwd`` — одно правило у Responses и Chat Completions."""

from __future__ import annotations

import os
from typing import Any

from gc_server.chat.errors import ApiError


def metadata_cwd(value: Any) -> str | None:
    """``metadata.cwd`` запроса: непустая строка или ``None``."""
    if not isinstance(value, dict):
        return None
    cwd = value.get("cwd")
    if not isinstance(cwd, str) or cwd == "":
        return None
    return cwd


def cwd_for_new(sent: str | None, launch_root: Any) -> str:
    """Каталог нового хода: присланный абсолютный — только внутри корня
    (``launch_root``, иначе каталог процесса), иначе сам корень.

    Каталог хода — периметр записи агента, а с хранилищем агента в базе
    ещё и ключ проекта (память, скиллы). Проверяется и отдаётся ходу
    раскрытый путь (симлинки и «..»): подмена компонента после проверки
    не уведёт ход за периметр."""
    root = _root(launch_root)
    if not isinstance(sent, str) or not os.path.isabs(sent):
        return root
    cwd = os.path.realpath(sent)
    boundary = os.path.realpath(root)
    if os.path.commonpath([boundary, cwd]) != boundary:
        raise ApiError(400, "invalid_request", "cwd is outside launch_root")
    return cwd


def same_cwd(sent: str, recorded: str) -> bool:
    """Тот же каталог: записан раскрытый путь, прислать можно любой."""
    return os.path.realpath(sent) == os.path.realpath(recorded)


def _root(launch_root: Any) -> str:
    if isinstance(launch_root, str) and os.path.isabs(launch_root):
        return os.path.normpath(launch_root)
    return os.path.normpath(os.getcwd())
