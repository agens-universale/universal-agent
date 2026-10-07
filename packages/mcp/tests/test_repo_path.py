"""Тесты repo_path для editable install."""

from __future__ import annotations

import sys

from recommended_mcp.runtime.repo_path import (
    ensure_repo_on_sys_path,
    repo_root,
    repo_root_from,
)


def test_repo_root_points_at_monorepo() -> None:
    root = repo_root()
    assert root is not None
    # Маркер менялся дважды. Сначала им был `recommended_mcp/` в корне — он
    # умер вместе с переездом рецептов в packages/mcp. Потом пара «packages/ +
    # pyproject.toml» — она ловила любое чужое монорепо. Теперь строгий:
    # packages/core/pyproject.toml, объявляющий name = "gc-core".
    assert (
        root / "packages" / "mcp" / "src" / "recommended_mcp" / "install.py"
    ).is_file()
    # После разбиения на модули mcp_runtime живёт в пакете ядра.
    assert (
        root / "packages" / "core" / "src" / "gc_core" / "mcp_runtime"
    ).is_dir()


def test_ensure_repo_puts_recommended_mcp_on_path() -> None:
    root = ensure_repo_on_sys_path()
    assert root is not None
    import recommended_mcp.install  # noqa: F401

    assert str(root) in sys.path


def test_foreign_monorepo_is_not_our_root(tmp_path) -> None:
    """Чужое монорепо со своим packages/*/src нашим корнем не объявляется.

    Слабый маркер («packages/ рядом с pyproject.toml») отдавал ЕГО корень, а
    ensure_repo_on_sys_path клал чужие каталоги ВПЕРЕДИ site-packages.
    """
    (tmp_path / "packages" / "core" / "src" / "foo").mkdir(parents=True)
    (tmp_path / "packages" / "core" / "pyproject.toml").write_text(
        '[project]\nname = "someone-else"\n', encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "someone-else-meta"\n', encoding="utf-8"
    )
    assert (
        repo_root_from(tmp_path / "packages" / "core" / "src" / "foo" / "m.py")
        is None
    )


def test_legacy_layout_still_found(tmp_path) -> None:
    """Дореформенная раскладка с единым <repo>/src.

    Ветка живая ТОЛЬКО в этой копии маркера: в таком дереве нет ни
    packages/core, ни gc_core, и единственный, кому там нужен корень, — прямой
    запуск install.py / run.py. Её легко снести при «унификации трёх копий»,
    поэтому она закреплена отдельно.
    """
    (tmp_path / "src" / "mcp_runtime").mkdir(parents=True)
    (tmp_path / "src" / "recommended_mcp").mkdir()
    start = tmp_path / "src" / "recommended_mcp" / "install.py"
    assert repo_root_from(start) == tmp_path.resolve()
