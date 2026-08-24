"""Гейт: ядро не зависит от ``requests``.

Уход с requests был отдельной целью — не ради асинхронности (у httpx есть
синхронный ``Client`` и переезд был механическим), а ради одной зависимости
вместо двух на один и тот же HTTP.

Тест сторожит откат с двух сторон: манифест и код. Проверка на импорт
сделана статически, а не через ``import requests`` в подпроцессе: в dev-окружении
requests всё равно стоит транзитивно (его тянут соседние пакеты), и
динамическая проверка там ничего не поймала бы.
"""
import ast
import tomllib
from pathlib import Path

import pytest

from gc_testkit.paths import REPO

_SRC = REPO / "packages" / "core" / "src" / "gc_core"
_MANIFEST = REPO / "packages" / "core" / "pyproject.toml"


def test_manifest_does_not_declare_requests():
    manifest = tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))
    deps = manifest["project"]["dependencies"]
    offenders = [d for d in deps if d.split("[")[0].strip().lower() == "requests"]
    assert not offenders, f"requests вернулся в зависимости gc-core: {offenders}"


def _modules_importing_requests() -> list[str]:
    """Модули ядра, где в КОДЕ есть import requests (не в комментарии)."""
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — синтаксис ловит другой гейт
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "requests" or n.startswith("requests.") for n in names):
                offenders.append(str(path.relative_to(_SRC)))
                break
    return sorted(set(offenders))


def test_no_module_imports_requests():
    """Ни один модуль ядра не импортирует requests — даже лениво, внутри функции.

    AST, а не поиск по тексту: слово «requests» законно встречается в
    докстроках, где объяснено, как было раньше и почему уехали.
    """
    offenders = _modules_importing_requests()
    assert offenders == [], f"requests импортируется в: {offenders}"


@pytest.mark.parametrize(
    "module",
    [
        "gc_core.aio",
        "gc_core.utils.http_session",
        "gc_core.utils.connect_backend",
        "gc_core.utils.no_proxy",
        "gc_core.utils.http_proxy",
        "gc_core.utils.backend_errors",
        "gc_core.utils.prod_auth",
        "gc_core.model_picker",
        "gc_core.runtime_notices",
        "gc_core.tools.connect_mcp.api",
        "gc_core.tools.connect_mcp.transport",
        "gc_core.tools.connect_mcp.registry",
    ],
)
def test_key_modules_import_cleanly(module):
    """Модули, которые раньше сидели на requests, поднимаются как есть."""
    import importlib

    assert importlib.import_module(module) is not None
