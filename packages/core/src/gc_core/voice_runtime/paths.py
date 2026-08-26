"""Пути voice-runtime в системных каталогах агента (gc_paths), по образу mcp_runtime."""

from __future__ import annotations

import os
from pathlib import Path


def _gc_data() -> Path:
    from gc_core.config_modules.env_vars import gc_paths

    return gc_paths()["data"]


def voice_data_dir() -> Path:
    """Постоянные артефакты voice: env speaches, pids, leases, state.json."""
    return _gc_data() / "voice"


def speaches_root() -> Path:
    """Клон speaches с его .venv: <gc_data>/voice/speaches."""
    return voice_data_dir() / "speaches"


def voice_pids_dir() -> Path:
    return voice_data_dir() / "pids"


def voice_leases_dir() -> Path:
    """Lease-файлы клиентов (light-switch): <pid>.lease живых TUI."""
    return voice_data_dir() / "leases"


def speaches_python() -> Path:
    """Интерпретатор .venv внутри клона speaches."""
    root = speaches_root() / ".venv"
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def voice_state_path() -> Path:
    return voice_data_dir() / "state.json"
