"""Снимок модулей ядра для тестов, подменяющих пакеты в ``sys.modules``.

Убирать за подменой повторным импортом нельзя: соседи по прогону держат
ссылки на исходные модули, взятые при сборке, и свежая копия в
``sys.modules`` развела бы их monkeypatch и код. Восстановление возвращает
ИМЕННО сохранённые объекты.
"""

import sys
import types


def _is_gc_module(name: str) -> bool:
    # gc_testkit — тоже: его хелперы, впервые загруженные под подменой,
    # держали бы ссылки на снятые модули ядра.
    return name.split(".", 1)[0] in ("gc_core", "gc_testkit")


def _unbind(mod: types.ModuleType, name: str, saved: dict) -> None:
    # Снятый модуль не должен остаться атрибутом сохранённого родителя:
    # строковый monkeypatch ("gc_core.x.y") дошёл бы по атрибутам до снятой
    # копии, а прод импортировал бы модуль заново.
    parent, _, child = name.rpartition(".")
    if getattr(saved.get(parent), child, None) is mod:
        delattr(saved[parent], child)


def save_gc_modules() -> dict[str, types.ModuleType]:
    """Запомнить загруженные модули ядра и тест-кита."""
    return {
        name: mod for name, mod in sys.modules.items() if _is_gc_module(name)
    }


def restore_gc_modules(saved: dict[str, types.ModuleType]) -> None:
    """Снять загруженное после снимка и вернуть сохранённые объекты."""
    for name in list(sys.modules):
        if _is_gc_module(name) and name not in saved:
            _unbind(sys.modules.pop(name), name, saved)
    sys.modules.update(saved)
    # Атрибут пакета-родителя — тоже: `import gc_core.tools.x as m` идёт по
    # атрибутам, и повторный импорт под подменой их переставил.
    for name, mod in saved.items():
        parent, _, child = name.rpartition(".")
        if parent in saved:
            setattr(saved[parent], child, mod)
