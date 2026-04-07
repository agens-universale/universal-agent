"""
Undo the tools-package stub injected by tests/simple_tools/conftest.py.

That conftest replaces ``sys.modules["tools"]`` with a bare ModuleType
before collection of *any* test file. Subagent tests need the real
``tools`` package (with ``build_tools_data`` etc.), so we re-import it
here at collection time.
"""

import importlib
import sys


def _restore_real_tools_package() -> None:
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)
    importlib.import_module("tools")


_restore_real_tools_package()
