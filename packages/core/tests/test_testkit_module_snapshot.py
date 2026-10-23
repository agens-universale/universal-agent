"""Снимок модулей ядра возвращает соседям по прогону те же объекты.

Тест, подменивший пакет тулов, раньше убирал за собой повторным импортом.
Соседи, взявшие ссылки при сборке, после этого патчили одну копию модуля, а
прод работал с другой.
"""

import importlib
import sys
import types

import gc_core.vision.notes as notes_module
from gc_testkit.module_snapshot import restore_gc_modules, save_gc_modules


def test_restore_returns_the_original_module_objects():
    saved = save_gc_modules()
    sys.modules.pop("gc_core.vision.notes")
    fresh = importlib.import_module("gc_core.vision.notes")
    assert fresh is not notes_module

    restore_gc_modules(saved)

    assert sys.modules["gc_core.vision.notes"] is notes_module
    assert sys.modules["gc_core.vision"].notes is notes_module


def test_restore_drops_modules_loaded_after_the_snapshot():
    saved = save_gc_modules()
    sys.modules["gc_core._after_snapshot"] = types.ModuleType("probe")

    restore_gc_modules(saved)

    assert "gc_core._after_snapshot" not in sys.modules


def test_restore_unbinds_dropped_modules_from_their_parents():
    """Строковый monkeypatch идёт по атрибутам пакетов: снятый модуль,
    оставшийся атрибутом родителя, принял бы подмену вместо настоящего."""
    import gc_core

    saved = save_gc_modules()
    probe = types.ModuleType("gc_core._after_snapshot")
    sys.modules["gc_core._after_snapshot"] = probe
    gc_core._after_snapshot = probe

    restore_gc_modules(saved)

    assert not hasattr(gc_core, "_after_snapshot")
