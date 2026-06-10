"""Пути MCP-runtime в системных каталогах агента (gc_paths)."""

from __future__ import annotations

import os
from pathlib import Path


def _gc_data_dir() -> Path:
    from config_modules.env_vars import gc_paths

    return gc_paths()["data"]


def recommended_mcp_data_dir() -> Path:
    """Постоянные артефакты MCP в data: venv, pids, runtime.json."""
    return _gc_data_dir() / "recommended_mcp"


def mcp_env_root() -> Path:
    """Корень venv для всех MCP: <gc_data>/recommended_mcp/venv."""
    return recommended_mcp_data_dir() / "venv"


def mcp_bin_dir() -> Path:
    """bin/ или Scripts/ внутри mcp-env."""
    root = mcp_env_root()
    return root / ("Scripts" if os.name == "nt" else "bin")


def mcp_python_executable() -> Path:
    """Интерпретатор внутри mcp-env."""
    root = mcp_env_root()
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def runtime_state_path() -> Path:
    """JSON со ссылкой на venv и последний манифест install."""
    return recommended_mcp_data_dir() / "runtime.json"


def recipe_root() -> Path:
    """Каталог рецептов recommended_mcp (сниппеты, external-пути в манифесте)."""
    from mcp_runtime.repo_path import ensure_repo_on_sys_path

    ensure_repo_on_sys_path()
    import recommended_mcp

    return Path(recommended_mcp.__file__).resolve().parent
