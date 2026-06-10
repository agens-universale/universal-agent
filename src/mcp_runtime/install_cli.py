"""Entry point gc-mcp-install → recommended_mcp.install."""

from __future__ import annotations


def main() -> int:
    from mcp_runtime.repo_path import ensure_repo_on_sys_path

    if ensure_repo_on_sys_path() is None:
        raise SystemExit(
            "recommended_mcp/ не найден. Запускай gc-mcp-install из editable install "
            "(uv tool install -e .) в каталоге mini-cli-pub."
        )
    from recommended_mcp.install import main as install_main

    return install_main()
