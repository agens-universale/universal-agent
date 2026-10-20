"""Каталог хода из ``metadata.cwd`` — одно правило у Responses и Chat Completions."""

from __future__ import annotations

import os
from typing import Any


def metadata_cwd(value: Any) -> str | None:
    """``metadata.cwd`` запроса: непустая строка или ``None``."""
    if not isinstance(value, dict):
        return None
    cwd = value.get("cwd")
    if not isinstance(cwd, str) or cwd == "":
        return None
    return cwd


def cwd_for_new(sent: str | None, launch_root: Any) -> str:
    """Каталог нового хода: присланный абсолютный, иначе ``launch_root``,
    иначе каталог процесса."""
    if isinstance(sent, str) and os.path.isabs(sent):
        return os.path.normpath(sent)
    if isinstance(launch_root, str) and os.path.isabs(launch_root):
        return os.path.normpath(launch_root)
    return os.path.normpath(os.getcwd())
