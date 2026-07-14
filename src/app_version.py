"""Версия агента и источник установки (direct_url.json, PEP 610).

Технический слой: только чтение метаданных, без бизнес-логики и без print.
uv-receipt.toml сознательно НЕ используется: не содержит коммита (проверено
живьём 2026-07-11), а direct_url.json пишет любой PEP 610-установщик.
"""
from __future__ import annotations

import json
from typing import Callable, NamedTuple, Optional

DIST_NAME = "gc-ua"


class InstallSource(NamedTuple):
    url: str
    ref: Optional[str]      # requested_revision; None = дефолтная ветка источника
    commit: Optional[str]   # зарезолвленный коммит установки
    editable: bool          # dev-установка из исходников


def agent_version() -> str:
    from importlib import metadata
    try:
        return metadata.version(DIST_NAME)
    except metadata.PackageNotFoundError:
        return "dev"


def _read_direct_url_text() -> Optional[str]:
    from importlib import metadata
    try:
        dist = metadata.distribution(DIST_NAME)
    except metadata.PackageNotFoundError:
        return None
    # read_text возвращает None, если файла нет (importlib сам гасит
    # FileNotFoundError/PermissionError); except — страховка от экзотики ФС.
    try:
        return dist.read_text("direct_url.json")
    except OSError:
        return None


def install_source(
    _read: Callable[[], Optional[str]] = _read_direct_url_text,
) -> Optional[InstallSource]:
    """Источник установки или None (нет direct_url.json / нечитаем)."""
    text = _read()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    url = data.get("url") or ""
    if not url:
        return None
    vcs = data.get("vcs_info") or {}
    dir_info = data.get("dir_info") or {}
    return InstallSource(
        url=url,
        ref=vcs.get("requested_revision"),
        commit=vcs.get("commit_id"),
        editable=bool(dir_info.get("editable")),
    )
