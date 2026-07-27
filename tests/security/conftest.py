import sys
import types
from pathlib import Path

import pytest
from _pytest.python import Module


def _stub_packages() -> dict:
    """Установить заглушку пакета `tools` и подпакетов builtin tools."""
    root = Path(__file__).resolve().parents[2]
    tools_dir = root / "src" / "tools"

    saved: dict = {}
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            saved[name] = sys.modules.pop(name)

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["tools"] = tools_pkg

    for sub in ("fs", "exec", "session", "deprecated"):
        subpkg = types.ModuleType(f"tools.{sub}")
        subpkg.__path__ = [str(tools_dir / sub)]
        sys.modules[f"tools.{sub}"] = subpkg
        setattr(tools_pkg, sub, subpkg)

    stored_pkg = types.ModuleType("tools.session.stored_outputs")
    stored_pkg.__path__ = [str(tools_dir / "session" / "stored_outputs")]
    sys.modules["tools.session.stored_outputs"] = stored_pkg
    setattr(sys.modules["tools.session"], "stored_outputs", stored_pkg)

    return saved


def _restore_packages(saved: dict) -> None:
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            del sys.modules[name]
    for name, module in saved.items():
        sys.modules[name] = module


class _StubbedToolsModule(Module):
    """`pytest.Module`, импортирующий тест с активной заглушкой `tools`."""

    def _getobj(self):
        saved = _stub_packages()
        try:
            if self.path.name == "test_trace_id_audit.py":
                # В tests/ (корень) есть одноимённый файл (вторая половина
                # после разделения теста trace_id на security/summary части).
                # Обычный mode=prepend требует уникальный basename по всему
                # дереву тестов и падает с "import file mismatch" при
                # совместном запуске. importlib-режим строит имя модуля по
                # полному пути, так что коллизии нет; ограничиваем его этим
                # файлом, чтобы не менять поведение импорта для остальных
                # тестов каталога.
                from _pytest.pathlib import import_path

                return import_path(
                    self.path,
                    mode="importlib",
                    root=self.config.rootpath,
                    consider_namespace_packages=self.config.getini(
                        "consider_namespace_packages"
                    ),
                )
            return super()._getobj()
        finally:
            _restore_packages(saved)


@pytest.hookimpl
def pytest_pycollect_makemodule(module_path, parent):
    return _StubbedToolsModule.from_parent(parent, path=module_path)
