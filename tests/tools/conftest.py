import sys
import types
from pathlib import Path

import pytest
from _pytest.python import Module


def _stub_packages() -> dict:
    """Установить заглушку пакета `tools` и подпакетов builtin tools.

    Возвращает снимок исходных записей `sys.modules` (имя -> объект-модуль)
    для `tools`/`tools.*`. Ключи, которых изначально не было, при восстановлении
    просто не возвращаются — отдельный sentinel не нужен.
    """
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
    """Откатить заглушку `tools`/`tools.*`, восстановив исходный `sys.modules`."""
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            del sys.modules[name]
    for name, module in saved.items():
        sys.modules[name] = module


class _StubbedToolsModule(Module):
    """`pytest.Module`, импортирующий тест с активной заглушкой `tools`.

    Заглушка нужна модулям `tests/tools/` на ВРЕМЯ импорта: без подмены
    выполнился бы реальный `src/tools/__init__.py` (MCP/skills/subagents).
    """

    def _getobj(self):
        saved = _stub_packages()
        try:
            return super()._getobj()
        finally:
            _restore_packages(saved)


@pytest.hookimpl
def pytest_pycollect_makemodule(module_path, parent):
    return _StubbedToolsModule.from_parent(parent, path=module_path)


def _stub_optional_web_parsers() -> None:
    if "trafilatura" not in sys.modules:
        mod = types.ModuleType("trafilatura")
        mod.extract = lambda *args, **kwargs: None
        sys.modules["trafilatura"] = mod

    if "readability" not in sys.modules:
        mod = types.ModuleType("readability")

        class _Document:
            def __init__(self, *_args, **_kwargs):
                pass

            def summary(self, html_partial=True):
                return ""

        mod.Document = _Document
        sys.modules["readability"] = mod


_stub_optional_web_parsers()
