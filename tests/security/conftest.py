import sys
import types
from pathlib import Path


def _stub_tools_package() -> None:
    root = Path(__file__).resolve().parents[2]
    src = root / "src"
    tools_dir = src / "tools"
    simple_tools_dir = tools_dir / "simple_tools"

    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["tools"] = tools_pkg

    simple_tools_pkg = types.ModuleType("tools.simple_tools")
    simple_tools_pkg.__path__ = [str(simple_tools_dir)]
    sys.modules["tools.simple_tools"] = simple_tools_pkg
    setattr(tools_pkg, "simple_tools", simple_tools_pkg)


_stub_tools_package()
