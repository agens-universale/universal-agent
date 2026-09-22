"""Создание MCP-venv: предпочитаем `uv venv` (корректно из uv-standalone Python,
где stdlib venv ломается на macOS — нет libpython*.dylib), фолбэк на stdlib;
битый/частичный venv пересоздаётся."""
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from recommended_mcp.runtime import venv as venv_mod

# --- _python_runs: health-check интерпретатора ---

def test_python_runs_true_for_real_interpreter():
    assert venv_mod._python_runs(Path(sys.executable)) is True


def test_python_runs_false_for_bogus_path():
    assert venv_mod._python_runs(Path("/no/such/python-xyz")) is False


# --- _create_mcp_venv: uv-first, stdlib-fallback ---

def test_create_prefers_uv(tmp_path):
    root = tmp_path / "venv"
    with patch("shutil.which", return_value="/usr/bin/uv"), \
         patch("subprocess.run") as run:
        venv_mod._create_mcp_venv(root)
    cmd = run.call_args.args[0]
    assert cmd[:3] == ["/usr/bin/uv", "venv", "--seed"]
    assert "--python" in cmd and sys.executable in cmd
    assert str(root) in cmd


def test_create_falls_back_to_stdlib_when_no_uv(tmp_path):
    root = tmp_path / "venv"
    with patch("shutil.which", return_value=None), \
         patch.object(venv_mod.venv, "EnvBuilder") as eb:
        venv_mod._create_mcp_venv(root)
    eb.return_value.create.assert_called_once_with(root)


def test_create_falls_back_when_uv_fails(tmp_path):
    root = tmp_path / "venv"
    with patch("shutil.which", return_value="/usr/bin/uv"), \
         patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "uv")), \
         patch.object(venv_mod.venv, "EnvBuilder") as eb:
        venv_mod._create_mcp_venv(root)
    eb.return_value.create.assert_called_once_with(root)


# --- ensure_mcp_venv: возврат рабочего / пересоздание битого ---

def test_ensure_returns_existing_working_venv(tmp_path):
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    with patch.object(venv_mod, "mcp_python_executable", return_value=py), \
         patch.object(venv_mod, "_python_runs", return_value=True), \
         patch.object(venv_mod, "_create_mcp_venv") as create:
        result = venv_mod.ensure_mcp_venv()
    assert result == py
    create.assert_not_called()


def test_ensure_recreates_broken_venv(tmp_path):
    root = tmp_path / "venv"
    py = root / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")  # python-файл есть, но «битый»
    runs = iter([False, True])  # до пересоздания не стартует, после — стартует
    with patch.object(venv_mod, "mcp_python_executable", return_value=py), \
         patch.object(venv_mod, "mcp_env_root", return_value=root), \
         patch.object(venv_mod, "_python_runs", side_effect=lambda p: next(runs)), \
         patch.object(venv_mod, "_create_mcp_venv") as create, \
         patch("shutil.rmtree") as rmtree:
        result = venv_mod.ensure_mcp_venv()
    rmtree.assert_called_once()   # битый снесён
    create.assert_called_once()   # пересоздан
    assert result == py
