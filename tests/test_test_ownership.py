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
#: Профили приложения зависят от gc-mcp (менеджер MCP), поэтому рецепты им
#: доступны. Ядру — нет: оно тянет их лениво и обязано работать без них.
ALLOWED = {
    "core": {"gc_core"},
    "mcp": {"gc_core", "recommended_mcp"},
    "cli": {"gc_core", "gc_cli", "recommended_mcp"},
    "server": {"gc_core", "gc_server", "recommended_mcp"},
    "tui": {"gc_core", "gc_tui", "recommended_mcp"},
}

OUR_PACKAGES = {"gc_core", "gc_cli", "gc_tui", "gc_server", "recommended_mcp"}


TESTKIT_SRC = REPO / "packages" / "testkit" / "src" / "gc_testkit"


def _dotted_imports(path) -> set[str]:
    """Полные точечные имена настоящих импортов файла.

    Строковые литералы НЕ считаем: иначе ложно срабатывает, например,
    packages/core/tests/test_missing_profile_guards.py — он проверяет шимы ЯДРА
    и упоминает "gc_tui"/"gc_server" строками, как имена пакетов, отсутствие
    которых имитирует.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
        ):
            found.add(node.module)
    return found


def _testkit_file(dotted: str):
    """Файл модуля тест-кита по точечному имени, иначе None.

    `gc_testkit.tui_support` → tui_support.py; `gc_testkit.helpers.x` →
    helpers/x.py; сам пакет `gc_testkit.helpers` → helpers/__init__.py.
    """
    parts = dotted.split(".")
    if parts[0] != "gc_testkit":
        return None
    rel = TESTKIT_SRC.joinpath(*parts[1:])
    for candidate in (rel.with_suffix(".py"), rel / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _testkit_pulls(dotted: str, _seen=None) -> set[str]:
    """Наши пакеты, которые ТЯНЕТ модуль тест-кита — включая транзитивно.

    Тест-кит не нейтрален: `tui_support` внутри функции импортирует
    `gc_tui.screens.settings`, `tui_fixtures` и `harbor_fakes` тянут `gc_core`.
    Пока сетка считала только прямые импорты набора, тест ядра мог написать
    `from gc_testkit.tui_support import open_settings_section`, остаться
    зелёным — и потребовать `gc_tui`. Ровно тот класс, ради которого тест-кит и
    вынесли из conftest'ов, и ровно то, что сетка объявлена доказывать.
    Проверено подменой: до этой правки такая строка в тесте ядра сетку не
    роняла.
    """
    if _seen is None:
        _seen = set()
    if dotted in _seen:  # циклы между модулями тест-кита
        return set()
    _seen.add(dotted)

    path = _testkit_file(dotted)
    if path is None:
        return set()

    pulls: set[str] = set()
    for imported in _dotted_imports(path):
        top = imported.split(".")[0]
        if top in OUR_PACKAGES:
            pulls.add(top)
        elif top == "gc_testkit":
            pulls |= _testkit_pulls(imported, _seen)
    return pulls


def _real_imports(path) -> set[str]:
    """Наши пакеты, которые требует файл, — напрямую или через тест-кит."""
    found = _dotted_imports(path)
    required = {name.split(".")[0] for name in found} & OUR_PACKAGES
    for imported in found:
        if imported.split(".")[0] == "gc_testkit":
            required |= _testkit_pulls(imported)
    return required


@pytest.mark.parametrize("dist", sorted(ALLOWED))
def test_suite_does_not_import_packages_above_it(dist):
    suite = REPO / "packages" / dist / "tests"
    if not suite.is_dir():
        pytest.skip(f"у {dist} нет набора тестов")

    allowed = ALLOWED[dist]
    offenders: list[str] = []
    scanned = 0
    for path in sorted(suite.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        scanned += 1
        extra = _real_imports(path) - allowed
        if extra:
            offenders.append(f"{path.relative_to(REPO)}: {sorted(extra)}")

    # Страховка от вакуума. Сетка утверждает ОТСУТСТВИЕ находок, а такое
    # утверждение верно и когда обход вернул пусто: переименовали каталог,
    # сменили маску, сломали rglob — и сетка навсегда зелёная, хотя не
    # проверяет уже ничего. У соседей (`module_literals`, пины рецептов MCP,
    # снапшот-гейт) такой счётчик есть, здесь его забыли.
    assert scanned, (
        f"в {suite.relative_to(REPO)} не просмотрено ни одного файла — сетка ничего не проверяет"
    )

    assert not offenders, (
        f"набор пакета {dist} импортирует чужие пакеты — автономный прогон "
        f"проходит только потому, что они есть в общем окружении:\n  "
        + "\n  ".join(offenders)
    )
