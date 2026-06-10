"""Тесты repo_path для editable install."""
from __future__ import annotations

import sys

from mcp_runtime.repo_path import ensure_repo_on_sys_path, repo_root


def test_repo_root_points_at_monorepo() -> None:
    root = repo_root()
    assert root is not None
    assert (root / "recommended_mcp" / "install.py").is_file()
    assert (root / "src" / "mcp_runtime").is_dir()


def test_ensure_repo_puts_recommended_mcp_on_path() -> None:
    root = ensure_repo_on_sys_path()
    assert root is not None
    import recommended_mcp.install  # noqa: F401

    assert str(root) in sys.path
