"""Pytest-плагин тест-кита — общий для наборов всех пакетов ``gc-*``.

Подключается автоматически через entry point ``pytest11``, поэтому отдельный
``conftest.py`` в каждом пакете не нужен: набор любого пакета получает и
заглушку ``tools``, и сбрасывающие состояние autouse-фикстуры.

Pytest: keep a consistent real ``tools`` package when MCP is installed.

Some tests replace ``sys.modules['tools']`` with stubs (summary_triggers, inject_tool_fields,
processing_responses stubs). Without cleanup, later tests see ``ModuleNotFoundError`` for
``tools.connect_mcp`` / ``tools.skills``.

When MCP is **not** installed, a minimal stub is registered at startup so intent tests can import
``states`` without running ``tools/__init__.py``.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

# Исходники разъехались по пакетам — раскладку знает gc_testkit.paths.
from .paths import REPO, module_dir, package_dir  # noqa: E402
from .sources import ensure_sources_on_path  # noqa: E402

# Набор может гоняться из корня репозитория или из каталога одного пакета;
# во втором случае каталоги исходников соседних пакетов на sys.path не попадут
# сами. Ставим их до первого импорта gc_*.
ensure_sources_on_path()

_CORE_SRC = package_dir("gc_core")
_TOOLS_DIR = module_dir("tools")
_MINIMAL_PRELOADED = False


def _isolate_user_config() -> None:
    """Увести конфиг-каталог в репозиторный, чтобы прогон не зависел от машины.

    Тесты, зовущие ``load_config()`` без явного пути, читали ЛИЧНЫЙ каталог
    разработчика (``~/.config/gc/``) со всеми оверлеями. Прогон переставал быть
    свойством репозитория.

    Сломалось это ровно так, как и должно было: в чужом ``config.local.yml``
    появилась секция ``voice:``, схема ``AppConfig`` её не знает, а лишние ключи
    запрещает (``extra_forbidden``). Один неизвестный ключ в файле, к
    репозиторию отношения не имеющем, уронил 51 тест.

    Почему здесь, а не в корневом ``conftest.py``: у каждого пакета свой
    ``pyproject.toml``, и при запуске подмножества (``pytest packages/core/tests``)
    pytest берёт rootdir там, а корневой conftest не грузит вовсе. Плагин
    тест-кита подключён через entry point ``pytest11`` и попадает в любой набор.

    ``setdefault``: явно заданный снаружи ``GC_CONFIG_DIR`` уважаем — это
    осознанное действие вызывающего.
    """
    fixture_dir = REPO / "tests" / "fixtures" / "gc-config"
    if fixture_dir.is_dir():
        os.environ.setdefault("GC_CONFIG_DIR", str(fixture_dir.resolve()))


# Вызов на импорте модуля: ``env_vars`` строит синглтон ``env = EnvVars()``
# при своём импорте, читая os.environ ровно один раз. Любая фикстура — даже
# session-scoped — отработает уже после и опоздает.
_isolate_user_config()


def _mcp_runtime_available() -> bool:
    try:
        importlib.import_module("mcp.client.session")
        return True
    except ImportError:
        return False


def _load_file_module(name: str, rel: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    path = _CORE_SRC / rel
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


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


def preload_minimal_tools_package() -> None:
    """Minimal ``tools`` tree for intent tests when MCP is not installed."""
    global _MINIMAL_PRELOADED
    if _MINIMAL_PRELOADED:
        return
    _MINIMAL_PRELOADED = True

    _stub_optional_web_parsers()
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            del sys.modules[name]

    tools_pkg = types.ModuleType("gc_core.tools")
    tools_pkg.__path__ = [str(_TOOLS_DIR)]
    sys.modules["gc_core.tools"] = tools_pkg

    session_pkg = types.ModuleType("gc_core.tools.session")
    session_pkg.__path__ = [str(_TOOLS_DIR / "session")]
    sys.modules["gc_core.tools.session"] = session_pkg
    setattr(tools_pkg, "session", session_pkg)

    stored_pkg = types.ModuleType("gc_core.tools.session.stored_outputs")
    stored_pkg.__path__ = [str(_TOOLS_DIR / "session" / "stored_outputs")]
    sys.modules["gc_core.tools.session.stored_outputs"] = stored_pkg
    setattr(session_pkg, "stored_outputs", stored_pkg)
    _load_file_module(
        "gc_core.tools.session.stored_outputs.tool_call_outputs_store",
        "tools/session/stored_outputs/tool_call_outputs_store.py",
    )
    wrapping_mod = types.ModuleType("gc_core.tools.session.stored_outputs.wrapping")
    wrapping_mod.maybe_store_and_wrap_tool_output = lambda tool_res, **_kw: tool_res
    sys.modules["gc_core.tools.session.stored_outputs.wrapping"] = wrapping_mod

    skills_pkg = types.ModuleType("gc_core.tools.skills")
    skills_pkg.__path__ = [str(_TOOLS_DIR / "skills")]
    sys.modules["gc_core.tools.skills"] = skills_pkg

    _load_file_module(
        "gc_core.tools.simple_tool_base", "tools/simple_tool_base.py")
    _load_file_module(
        "gc_core.tools.turn.submit_solution",
        "tools/turn/submit_solution.py",
    )

    connect_mcp_pkg = types.ModuleType("gc_core.tools.connect_mcp")
    connect_mcp_pkg.__path__ = [str(_TOOLS_DIR / "connect_mcp")]
    sys.modules["gc_core.tools.connect_mcp"] = connect_mcp_pkg


def _real_tools_loaded() -> bool:
    tools = sys.modules.get("gc_core.tools")
    if tools is None:
        return False
    return (
        callable(getattr(tools, "build_tools_data", None))
        and "gc_core.tools.connect_mcp" in sys.modules
        and "gc_core.tools.skills" in sys.modules
        and isinstance(getattr(sys.modules.get("gc_core.tools.skills"), "__path__", None), list)
        and len(sys.modules["gc_core.tools.skills"].__path__) > 0
    )


def ensure_real_tools_package() -> None:
    """Reload full ``tools`` from ``src/tools`` (requires MCP and project deps)."""
    if _real_tools_loaded():
        return
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            del sys.modules[name]
    importlib.import_module("gc_core.tools")


if not _mcp_runtime_available():
    preload_minimal_tools_package()


@pytest.fixture(autouse=True)
def _restore_real_tools_package_after_test():
    yield
    if _mcp_runtime_available():
        ensure_real_tools_package()


@pytest.fixture(autouse=True)
def _clear_runtime_notices_between_tests():
    from gc_core.runtime_notices import clear_runtime_notices

    clear_runtime_notices(listeners=True)
    yield
    clear_runtime_notices(listeners=True)


@pytest.fixture(autouse=True)
def _reset_message_projection_tags():
    """Спека 08: каждый тест стартует с default for_model = только think."""
    from gc_core.message_projection import configure_for_model_tags

    configure_for_model_tags([])
    yield
    configure_for_model_tags([])
