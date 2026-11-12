"""Снимок модулей ядра на время теста, подменяющего пакет тулов.

Тест кладёт в ``sys.modules`` заглушку ``gc_core.tools`` и загружает под
ней исполнителя заново. Вернуть надо ровно прежние модули: код, загруженный
до теста (в том числе при сборке тестов), держит их, и свежая копия в
``sys.modules`` развела бы подмены (``monkeypatch`` по модулю) и прод.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import gc_core

_MISSING = object()
_DEPENDENTS = ("gc_core.agent_executor", "gc_core.run_summary_coordinator")
_PACKAGE_ATTRS = ("tools", "agent_executor", "run_summary_coordinator")
_PROCESSING_ATTRS = ("aapply_tool", "WrongFunctionArgumentsException")


def _tracked(name: str) -> bool:
    return (
        name in _DEPENDENTS
        or name == "gc_core.tools"
        or name.startswith("gc_core.tools.")
    )


class ModuleSnapshot:
    """Модули тулов и зависимые от них — как были до подмены."""

    def __init__(self) -> None:
        self._modules = {
            name: module
            for name, module in sys.modules.items()
            if _tracked(name)
        }
        self._attrs = {
            name: getattr(gc_core, name, _MISSING) for name in _PACKAGE_ATTRS
        }
        processing = sys.modules.get("gc_core.processing_responses")
        self._processing: dict[str, Any] = (
            {}
            if processing is None
            else {
                name: getattr(processing, name) for name in _PROCESSING_ATTRS
            }
        )

    def restore(self) -> None:
        for name in [name for name in sys.modules if _tracked(name)]:
            if name not in self._modules:
                del sys.modules[name]
        sys.modules.update(self._modules)
        for name, value in self._attrs.items():
            if value is not _MISSING:
                setattr(gc_core, name, value)
            elif hasattr(gc_core, name):
                delattr(gc_core, name)
        self._restore_processing()

    def _restore_processing(self) -> None:
        processing = sys.modules.get("gc_core.processing_responses")
        if processing is None:
            return
        if self._processing:
            for name, value in self._processing.items():
                setattr(processing, name, value)
            return
        # processing_responses впервые загружен под заглушкой: его ссылки
        # на тулы — на неё. Вернуть настоящие.
        real = importlib.import_module("gc_core.tools")
        for name in _PROCESSING_ATTRS:
            setattr(processing, name, getattr(real, name))
