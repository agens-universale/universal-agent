"""Тесты repo_path для editable install."""
from __future__ import annotations

import sys

from recommended_mcp.runtime.repo_path import ensure_repo_on_sys_path, repo_root


def test_repo_root_points_at_monorepo() -> None:
    root = repo_root()
    assert root is not None
    # Рецепты уехали в свой дистрибутив; маркером корня стала пара
    # «packages/ + pyproject.toml» вместо «recommended_mcp/ в корне».
    assert (root / "packages" / "mcp" / "src" / "recommended_mcp" / "install.py").is_file()
    # После разбиения на модули mcp_runtime живёт в пакете ядра.
    assert (root / "packages" / "core" / "src" / "gc_core" / "mcp_runtime").is_dir()


def test_ensure_repo_puts_recommended_mcp_on_path() -> None:
    root = ensure_repo_on_sys_path()
    assert root is not None
    import recommended_mcp.install  # noqa: F401

    assert str(root) in sys.path
