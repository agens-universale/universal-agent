"""Набор пакета не должен импортировать пакеты выше себя по зависимостям.

Ради этого разбиение и делалось: пока тесты общие, утверждение «ядро
самодостаточно» не проверяется ничем. После разделения его проверяет команда
`cd packages/core && pytest` — но только если в наборе ядра действительно нет
импортов фронтендов.

Ревизия 18.08.2026 нашла три таких файла, и все три проходили: gc-tui стоит в
общем рабочем окружении, поэтому импорт резолвился. То есть «зелёный автономный
прогон» ничего не доказывал. Этот тест закрывает дыру: он смотрит НА ИМПОРТЫ, а
не на результат прогона.

Правило: файл может импортировать свой пакет и всё, от чего его дистрибутив
зависит. gc-cli / gc-tui / gc-server зависят от gc-core, поэтому ядро им
доступно; обратное — нет.
"""

from __future__ import annotations

import ast

import pytest

from gc_testkit.paths import REPO

#: Что каждому набору разрешено импортировать помимо gc_testkit и сторонних.
ALLOWED = {
    "core": {"gc_core"},
    "mcp": {"gc_core", "recommended_mcp"},
    "cli": {"gc_core", "gc_cli"},
    "server": {"gc_core", "gc_server"},
    "tui": {"gc_core", "gc_tui"},
}

OUR_PACKAGES = {"gc_core", "gc_cli", "gc_tui", "gc_server", "recommended_mcp"}


def _real_imports(path) -> set[str]:
    """Только настоящие импорты. Строковые литералы НЕ считаем.

    Иначе ложно срабатывает, например, packages/core/tests/test_missing_profile_guards.py:
    он проверяет шимы ЯДРА и упоминает "gc_tui"/"gc_server" строками — как имена
    пакетов, отсутствие которых имитирует.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found & OUR_PACKAGES


@pytest.mark.parametrize("dist", sorted(ALLOWED))
def test_suite_does_not_import_packages_above_it(dist):
    suite = REPO / "packages" / dist / "tests"
    if not suite.is_dir():
        pytest.skip(f"у {dist} нет набора тестов")

    allowed = ALLOWED[dist]
    offenders: list[str] = []
    for path in sorted(suite.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        extra = _real_imports(path) - allowed
        if extra:
            offenders.append(f"{path.relative_to(REPO)}: {sorted(extra)}")

    assert not offenders, (
        f"набор пакета {dist} импортирует чужие пакеты — автономный прогон "
        f"проходит только потому, что они есть в общем окружении:\n  "
        + "\n  ".join(offenders)
    )
