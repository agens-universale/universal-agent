"""Создание и использование отдельного venv для MCP."""

from __future__ import annotations

import shutil
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


def _python_runs(py: Path) -> bool:
    """True, если интерпретатор реально стартует (`python -c ''` → 0).

    Ловит битый venv: stdlib-venv из uv-managed standalone Python на macOS даёт
    python, который не находит libpython*.dylib и падает с SIGABRT ещё до pip."""
    try:
        return subprocess.run(
            [str(py), "-c", ""], capture_output=True, timeout=15
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _create_mcp_venv(root: Path) -> None:
    """Создать venv в root.

    Предпочитаем `uv venv`: uv сам ставит standalone-Python и корректно строит из
    него venv (stdlib venv ломается на macOS — не переносит libpython*.dylib).
    `--seed` кладёт pip в venv (дальше зовём `python -m pip install`). Фолбэк —
    stdlib venv, когда uv недоступен: тогда базовый python обычный и stdlib ок.
    Корректность фолбэка: standalone-python ⟹ uv установлен (он же его и поставил),
    поэтому «битый» кейс всегда покрыт uv-веткой."""
    uv = shutil.which("uv")
    if uv:
        try:
            subprocess.run(
                [uv, "venv", "--seed", "--python", sys.executable, str(root)],
                check=True, capture_output=True, text=True,
            )
            return
        except (subprocess.CalledProcessError, OSError):
            pass  # uv не справился — пробуем stdlib ниже
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(root)
    except Exception as exc:
        raise McpVenvError(f"не удалось создать venv {root}: {exc}") from exc


def ensure_mcp_venv() -> Path:
    """Создать mcp-env при необходимости; вернуть путь к python внутри venv.

    Если venv уже есть, но его python не запускается (битый/частичный), venv
    пересоздаётся начисто — иначе сломанный интерпретатор переиспользовался бы."""
    py = mcp_python_executable()
    if py.is_file() and _python_runs(py):
        return py
    root = mcp_env_root()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.parent.mkdir(parents=True, exist_ok=True)
    _create_mcp_venv(root)
    if not (py.is_file() and _python_runs(py)):
        raise McpVenvError(f"venv создан, но python не запускается: {py}")
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


def pip_argv(*args: str) -> list[str]:
    """Произвольная pip-команда в активном MCP-интерпретаторе (install/uninstall/...)."""
    return [str(active_pip_python()), "-m", "pip", *args]


def query_installed_version(distribution_name: str) -> str | None:
    script = (
        "import importlib.metadata as m; "
        f"print(m.version({distribution_name!r}))"
    )
    argv = [str(active_pip_python()), "-c", script]
    from utils.win_console import preserve_console_mode
    with preserve_console_mode():   # зовётся из воркеров при живом TUI: subprocess на
        result = subprocess.run(     # Windows сбрасывает режим консоли (мышь, Ctrl+C)
            argv,
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
