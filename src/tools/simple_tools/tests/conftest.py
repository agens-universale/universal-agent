import sys
import types
from pathlib import Path


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[4]
    src = root / "src"
    src_str = str(src)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


_ensure_src_on_path()


def _stub_packages() -> None:
    root = Path(__file__).resolve().parents[4]
    src = root / "src"
    tools_dir = src / "tools"
    simple_tools_dir = tools_dir / "simple_tools"

    if "tools" not in sys.modules:
        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = [str(tools_dir)]
        sys.modules["tools"] = tools_pkg

    if "tools.simple_tools" not in sys.modules:
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
