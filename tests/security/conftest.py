import sys
import types
from pathlib import Path

import pytest
from _pytest.python import Module


def _stub_packages() -> dict:
    """Установить заглушку пакета `tools`/`tools.simple_tools`.

    Возвращает снимок исходных записей `sys.modules` (имя -> объект-модуль)
    для `tools`/`tools.*`. Ключи, которых изначально не было, при восстановлении
    просто не возвращаются — отдельный sentinel не нужен.
    """
    # Этот conftest лежит в `tests/security/`, а `src/` находится в корне репозитория.
    root = Path(__file__).resolve().parents[2]
    src = root / "src"
    tools_dir = src / "tools"
    simple_tools_dir = tools_dir / "simple_tools"

    # Снимок исходных модулей `tools` / `tools.*` ДО подмены, чтобы вернуть
    # ровно те же объекты (или удалить ключи, которых не было).
    saved: dict = {}
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            saved[name] = sys.modules.pop(name)

    # Принудительно переопределяем пакет `tools`, чтобы избежать побочных эффектов
    # от реального `src/tools/__init__.py` и конкуренции с внешними модулями.
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["tools"] = tools_pkg

    st_pkg = types.ModuleType("tools.simple_tools")
    st_pkg.__path__ = [str(simple_tools_dir)]
    sys.modules["tools.simple_tools"] = st_pkg
    setattr(sys.modules["tools"], "simple_tools", st_pkg)

    return saved


def _restore_packages(saved: dict) -> None:
    """Откатить заглушку `tools`/`tools.*`, восстановив исходный `sys.modules`.

    Удаляем все записи `tools`/`tools.*`, появившиеся под заглушкой, и
    возвращаем ровно те объекты, что были до подмены (включая отсутствие).
    """
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            del sys.modules[name]
    for name, module in saved.items():
        sys.modules[name] = module


class _StubbedToolsModule(Module):
    """`pytest.Module`, импортирующий тест с активной заглушкой `tools`.

    Заглушка нужна модулям `tests/security/` на ВРЕМЯ импорта: они делают
    `from tools.simple_tools.<sub> import ...` на верхнем уровне, и без подмены
    выполнился бы реальный `src/tools/__init__.py` (MCP/skills/subagents).
    Подмена живёт только на время импорта конкретного модуля и сразу
    откатывается — поэтому остальная сессия видит настоящий пакет `tools`.
    """

    def _getobj(self):
        saved = _stub_packages()
        try:
            return super()._getobj()
        finally:
            _restore_packages(saved)


@pytest.hookimpl
def pytest_pycollect_makemodule(module_path, parent):
    # Этот хук, объявленный в conftest `tests/security/`, вызывается только
    # для модулей данного подкаталога — заглушка не утекает за его пределы.
    return _StubbedToolsModule.from_parent(parent, path=module_path)
