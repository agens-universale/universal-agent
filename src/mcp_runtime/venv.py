"""Создание и использование отдельного venv для MCP."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from mcp_runtime.paths import mcp_env_root
from mcp_runtime.paths import mcp_python_executable


class McpVenvError(RuntimeError):
    """Не удалось подготовить MCP venv."""


_pip_target: Path | None = None


def ensure_mcp_venv() -> Path:
    """Создать mcp-env при необходимости; вернуть путь к python внутри venv."""
    py = mcp_python_executable()
    if py.is_file():
        return py
    root = mcp_env_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    builder = venv.EnvBuilder(with_pip=True, clear=False)
    try:
        builder.create(root)
    except Exception as exc:
        raise McpVenvError(f"не удалось создать venv {root}: {exc}") from exc
    if not py.is_file():
        raise McpVenvError(f"venv создан, но python не найден: {py}")
    return py


def active_pip_python() -> Path:
    return _pip_target or ensure_mcp_venv()


@contextmanager
def pip_install_target(py: Path | None = None) -> Iterator[Path]:
    """Направить pip install в указанный (или mcp-env) интерпретатор."""
    global _pip_target
    target = py or ensure_mcp_venv()
    previous = _pip_target
    _pip_target = target
    try:
        yield target
    finally:
        _pip_target = previous


def pip_install_argv(*install_args: str) -> list[str]:
    return [str(active_pip_python()), "-m", "pip", "install", *install_args]


def query_installed_version(distribution_name: str) -> str | None:
    script = (
        "import importlib.metadata as m; "
        f"print(m.version({distribution_name!r}))"
    )
    result = subprocess.run(
        [str(active_pip_python()), "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
