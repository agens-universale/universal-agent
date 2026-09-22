"""Три копии маркера «корень рабочей копии» обязаны совпадать.

Копий именно три, и это осознанно, а не забывчиво. Каноническая реализация —
`gc_core.bundled_assets.repo_root_from`; две другие живут в
`recommended_mcp.runtime.repo_path` и `gc_testkit.paths`, и обе работают ДО
того, как соседние дистрибутивы попадают на `sys.path`: бутстрап
`install.py`/`run.py` кладёт туда gc_core сам, а плагин тест-кита кладёт туда
`packages/*/src`. Импортировать каноническую им физически нечем.

Раньше копий было пять и с тремя разными контрактами (одна бросала, три
возвращали None, одна знала дореформенную раскладку), а маркером была пара
«каталог packages/ рядом с pyproject.toml». Она ловила ЛЮБОЕ чужое монорепо:
у проекта с `packages/*/src` и нашими колёсами в .venv корнем объявлялся ЕГО
корень — сервер брал чужой config_server.yml, установщик MCP шёл искать
external/vendor в чужом дереве. Маркер стал строгим:
`packages/core/pyproject.toml`, объявляющий `name = "gc-core"`.

Этот файл стережёт две вещи разом: что маркер не ослабили обратно и что три
копии не разъехались.
"""

from __future__ import annotations

import pytest
from gc_core import bundled_assets
from gc_testkit import paths as testkit_paths
from gc_testkit.paths import REPO
from recommended_mcp.runtime import repo_path

#: (имя для отчёта, модуль). Все три обязаны вести себя одинаково.
IMPLS = [
    ("gc_core.bundled_assets", bundled_assets),
    ("recommended_mcp.runtime.repo_path", repo_path),
    ("gc_testkit.paths", testkit_paths),
]


def _fake_monorepo(root, core_name: str):
    """Чужое монорепо: packages/*/src, корневой pyproject — всё как у нас.

    Отличается ровно одним: именем в packages/core/pyproject.toml. Старый
    слабый маркер объявил бы такое дерево нашим корнем.
    """
    (root / "packages" / "core" / "src" / "foo").mkdir(parents=True)
    (root / "packages" / "core" / "pyproject.toml").write_text(
        f'[project]\nname = "{core_name}"\nversion = "1.0"\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text('[project]\nname = "someone-else-meta"\n', encoding="utf-8")
    return root / "packages" / "core" / "src" / "foo" / "mod.py"


@pytest.mark.parametrize("name,mod", IMPLS, ids=[n for n, _ in IMPLS])
def test_all_impls_agree_on_the_real_repo(name, mod):
    assert mod.repo_root_from(bundled_assets.__file__) == REPO
    assert mod.repo_root() == REPO


@pytest.mark.parametrize("name,mod", IMPLS, ids=[n for n, _ in IMPLS])
def test_foreign_monorepo_is_not_our_root(name, mod, tmp_path):
    """Слабый маркер вернул бы tmp_path. Строгий обязан вернуть None."""
    inside = _fake_monorepo(tmp_path, "someone-else")
    assert mod.repo_root_from(inside) is None


@pytest.mark.parametrize("name,mod", IMPLS, ids=[n for n, _ in IMPLS])
def test_marker_actually_fires_not_just_always_none(name, mod, tmp_path):
    """То же дерево, но с нашим именем ядра — корень обязан найтись.

    Без этой половины предыдущий тест проходил бы и у функции `return None`.
    """
    inside = _fake_monorepo(tmp_path, "gc-core")
    assert mod.repo_root_from(inside) == tmp_path.resolve()


def test_three_copies_have_not_drifted():
    """Регулярка маркера — дословно одна и та же во всех трёх копиях."""
    patterns = {name: mod._GC_CORE_NAME.pattern for name, mod in IMPLS}
    assert len(set(patterns.values())) == 1, patterns
