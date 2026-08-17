import sys
import types
from pathlib import Path

import pytest
from _pytest.python import Module
from tests.paths import module_dir


def _stub_packages() -> dict:
    """Установить заглушку пакета `tools` и подпакетов builtin tools."""
    root = Path(__file__).resolve().parents[2]
    tools_dir = module_dir("tools")

    saved: dict = {}
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            saved[name] = sys.modules.pop(name)

    tools_pkg = types.ModuleType("gc_core.tools")
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["gc_core.tools"] = tools_pkg

    for sub in ("fs", "exec", "session", "deprecated"):
        subpkg = types.ModuleType(f"gc_core.tools.{sub}")
        subpkg.__path__ = [str(tools_dir / sub)]
        sys.modules[f"gc_core.tools.{sub}"] = subpkg
        setattr(tools_pkg, sub, subpkg)

    stored_pkg = types.ModuleType("gc_core.tools.session.stored_outputs")
    stored_pkg.__path__ = [str(tools_dir / "session" / "stored_outputs")]
    sys.modules["gc_core.tools.session.stored_outputs"] = stored_pkg
    setattr(sys.modules["gc_core.tools.session"], "stored_outputs", stored_pkg)

    return saved


def _restore_packages(saved: dict) -> None:
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            del sys.modules[name]
    for name, module in saved.items():
        sys.modules[name] = module


class _StubbedToolsModule(Module):
    """`pytest.Module`, импортирующий тест с активной заглушкой `tools`."""

    def _getobj(self):
        saved = _stub_packages()
        try:
            return super()._getobj()
        finally:
            _restore_packages(saved)


@pytest.hookimpl
def pytest_pycollect_makemodule(module_path, parent):
    return _StubbedToolsModule.from_parent(parent, path=module_path)
