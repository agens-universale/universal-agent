"""Промпты и скиллы по умолчанию в хранилище агента: метка версии пакета.

``gc-stores sync --with-defaults`` кладёт в хранилище промпты и системные
скиллы из пакета gc-core с меткой ``bundled@<версия>``. После обновления
gc-ua промпты пакета меняются вместе с тулами — копия в базе застывает.
Хранилище, прочитав умолчание другой версии, один раз предупреждает: нужно
выполнить ``gc-stores sync --with-defaults`` снова.
"""

from __future__ import annotations

import logging
from importlib.metadata import version

_log = logging.getLogger("gc_stores")
_PREFIX = "bundled@"


def bundled_source() -> str:
    """Метка умолчания пакета установленной версии gc-core."""
    return _PREFIX + version("gc-core")


class StaleDefaults:
    """Предупреждение об устаревших умолчаниях — одно на хранилище и
    версию."""

    def __init__(self, store_label: str) -> None:
        self._label = store_label
        self._warned: set[str] = set()

    def check(self, source: str | None) -> None:
        if not source or not source.startswith(_PREFIX):
            return
        current = bundled_source()
        if source == current or source in self._warned:
            return
        self._warned.add(source)
        _log.warning(
            "промпты и скиллы по умолчанию в хранилище %s — от gc-core %s, "
            "установлен %s: выполните gc-stores sync --with-defaults",
            self._label,
            source.removeprefix(_PREFIX),
            current.removeprefix(_PREFIX),
        )
