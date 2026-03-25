import sys
import types
from pathlib import Path


def _stub_packages() -> None:
    # Этот conftest лежит в `tests/simple_tools/`, а `src/` находится в корне репозитория.
    root = Path(__file__).resolve().parents[2]
    src = root / "src"
    tools_dir = src / "tools"
    simple_tools_dir = tools_dir / "simple_tools"

    # Принудительно переопределяем пакет `tools`, чтобы избежать побочных эффектов
    # от реального `src/tools/__init__.py` и конкуренции с внешними модулями.
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["tools"] = tools_pkg

    st_pkg = types.ModuleType("tools.simple_tools")
    st_pkg.__path__ = [str(simple_tools_dir)]
    sys.modules["tools.simple_tools"] = st_pkg
    setattr(sys.modules["tools"], "simple_tools", st_pkg)


_stub_packages()


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
