"""Ядро не импортирует то, чего не объявляет.

История повторилась дважды. `requests` сняли из зависимостей ядра, а голос
gc-tui продолжал его импортировать — чистая установка `gc-voice` падала на
импорте. Вторым тем же способом жил `urllib3`: ядро его импортировало, в
зависимостях его не было, приезжал транзитом через trafilatura, а вызов
`disable_warnings` после переезда на httpx ничего не делал.

Имя модуля и имя дистрибутива — разные вещи (`yaml` из PyYAML, `readability`
из readability-lxml), поэтому сверяем через метаданные установленных пакетов, а
не по строке: наивная проверка дала бы ложные срабатывания и была бы выключена
первой же правкой.
"""

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions

from gc_testkit.paths import REPO

CORE_SRC = REPO / "packages/core/src/gc_core"
CORE_MANIFEST = REPO / "packages/core/pyproject.toml"

#: Пакеты монорепозитория: их ядро видит через рабочую копию, не через PyPI.
_OWN = {"gc_core", "gc_mcp", "gc_cli", "gc_tui", "gc_server", "recommended_mcp", "gc_testkit"}


def _norm(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _declared() -> set:
    deps = tomllib.loads(CORE_MANIFEST.read_text(encoding="utf-8"))["project"]["dependencies"]
    out = set()
    for dep in deps:
        name = dep
        for sep in ("[", ">", "<", "=", "!", "~", ";", " "):
            name = name.split(sep)[0]
        out.add(_norm(name))
    return out


def _module_to_distributions() -> dict:
    return {mod: {_norm(d) for d in dists} for mod, dists in packages_distributions().items()}


def _third_party_imports() -> dict:
    """{модуль верхнего уровня: путь первого файла, где он импортируется}."""
    found: dict = {}
    for path in CORE_SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name in _OWN or name in sys.stdlib_module_names:
                    continue
                found.setdefault(name, path)
    return found


def test_urllib3_is_gone():
    """Точечно про urllib3: он и был находкой."""
    assert "urllib3" not in _third_party_imports(), (
        "ядро снова импортирует urllib3: он не объявлен и приезжает транзитом, а после переезда на httpx не нужен вовсе"
    )


def test_every_third_party_import_is_declared():
    declared = _declared()
    mapping = _module_to_distributions()

    undeclared = {}
    for module, path in _third_party_imports().items():
        dists = mapping.get(module) or {_norm(module)}
        if not (dists & declared):
            undeclared[module] = f"{path.relative_to(REPO)} (дистрибутив: {sorted(dists)})"

    assert not undeclared, f"ядро импортирует необъявленное — на чистой установке это падение на импорте: {undeclared}"
