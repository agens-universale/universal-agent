"""
Undo the tools-package stub injected by tests/tools/conftest.py.

That conftest replaces ``sys.modules["tools"]`` with a bare ModuleType
before collection of *any* test file. Subagent tests need the real
``tools`` package (with ``build_tools_data`` etc.), so we re-import it
here at collection time.
"""

import importlib
import sys


def _restore_real_tools_package() -> None:
    # No-op when `tools` is already the real, file-backed package (the
    # common case: tests/tools/ and tests/security/ conftests always
    # round-trip back to the real modules after each file they wrap).
    # Popping+reimporting unconditionally would also discard already-real
    # submodules (e.g. lazily-imported `tools.backends.local`) that other
    # already-collected test modules hold direct references to, creating a
    # second, distinct module/class object under the same dotted name and
    # breaking isinstance()/identity checks against the earlier reference.
    current = sys.modules.get("tools")
    if current is not None and getattr(current, "__file__", None):
        return
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)
    importlib.import_module("tools")


_restore_real_tools_package()
