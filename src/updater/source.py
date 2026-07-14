"""Идентификация установки для update-flow (спека D). Обёртка над A2
(direct_url.json). dev/editable или отсутствие direct_url → обновление
недоступно (обновляйтесь через git)."""
from __future__ import annotations

from typing import Callable, NamedTuple, Optional

import app_version


class UpdateSource(NamedTuple):
    url: str
    ref: Optional[str]      # None = дефолтная ветка (проверяем HEAD)
    commit: Optional[str]   # текущий установленный коммит


def resolved_source(
    _source: Callable[[], Optional["app_version.InstallSource"]] = app_version.install_source,
) -> Optional[UpdateSource]:
    src = _source()
    if src is None or src.editable or not src.url:
        return None
    return UpdateSource(url=src.url, ref=src.ref, commit=src.commit)
