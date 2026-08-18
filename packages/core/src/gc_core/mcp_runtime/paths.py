"""Пути MCP-runtime в системных каталогах агента (gc_paths)."""

from __future__ import annotations

import os
from pathlib import Path


def _gc_data_dir() -> Path:
    from gc_core.config_modules.env_vars import gc_paths

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


def managed_config_path() -> Path:
    """Реэкспорт из config_modules.env_vars — там теперь дом этой функции.

    Оставлен, чтобы не менять вызовы внутри MCP-подсистемы (wiring и др.).
    Путь принадлежит конфигу, а не MCP: раньше config.py статически импортировал
    его отсюда, и ядро конфига зависело от MCP-подсистемы.
    """
    from gc_core.config_modules.env_vars import managed_config_path as _impl

    return _impl()


def recipe_root() -> Path:
    """Каталог рецептов recommended_mcp (сниппеты, external-пути в манифесте)."""
    from gc_core.mcp_runtime.recipes import recipes_package

    # Через охранник: без рецептов получим RecipesUnavailable, а не сырой
    # ModuleNotFoundError. Именно recipes_package(), а не import_recipes():
    # второй ходит через importlib.import_module, который тесты подменяют
    # глобально, и тогда сюда прилетает MagicMock без __file__.
    return Path(recipes_package().__file__).resolve().parent
