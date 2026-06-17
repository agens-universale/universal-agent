"""Тесты отдельного MCP-runtime (venv в gc_paths)."""
from __future__ import annotations

import json
from pathlib import Path

from config_modules import env_vars
from mcp_runtime.env import augment_process_env, resolve_mcp_command
from mcp_runtime.paths import mcp_bin_dir, mcp_env_root, runtime_state_path
from mcp_runtime.state import load_runtime_state, save_runtime_state


def test_paths_under_gc_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_data_dir=tmp_path / "data"))
    assert mcp_env_root() == (tmp_path / "data" / "recommended_mcp" / "venv").resolve()
    assert runtime_state_path() == (tmp_path / "data" / "recommended_mcp" / "runtime.json").resolve()


def test_save_and_load_runtime_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_data_dir=tmp_path / "data"))
    bin_dir = mcp_bin_dir()
    bin_dir.mkdir(parents=True)
    manifest = tmp_path / "manifest.yml"
    manifest.write_text("mcps: []\n", encoding="utf-8")
    save_runtime_state(manifest_path=manifest)
    loaded = load_runtime_state()
    assert loaded is not None
    assert loaded.bin_dir == bin_dir
    assert loaded.manifest_path == manifest.resolve()
    payload = json.loads(runtime_state_path().read_text(encoding="utf-8"))
    assert payload["bin_dir"] == str(bin_dir)


def test_resolve_command_prefers_mcp_bin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_data_dir=tmp_path / "data"))
    bin_dir = mcp_bin_dir()
    bin_dir.mkdir(parents=True)
    script = bin_dir / ("office-mcp.cmd" if __import__("os").name == "nt" else "office-mcp")
    script.write_text("", encoding="utf-8")
    if __import__("os").name != "nt":
        script.chmod(0o755)
    save_runtime_state(manifest_path=None)
    assert resolve_mcp_command("office-mcp").endswith("office-mcp")


def test_augment_process_env_prepends_bin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_data_dir=tmp_path / "data"))
    bin_dir = mcp_bin_dir()
    bin_dir.mkdir(parents=True)
    save_runtime_state(manifest_path=None)
    env = augment_process_env({"PATH": "/usr/bin"})
    assert str(bin_dir) in env["PATH"].split(__import__("os").pathsep)


def test_managed_config_path(monkeypatch, tmp_path):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {"config": tmp_path / "cfg",
                                                       "data": tmp_path / "d",
                                                       "cache": tmp_path / "c",
                                                       "state": tmp_path / "s"})
    from mcp_runtime.paths import managed_config_path
    assert managed_config_path() == (tmp_path / "cfg" / "config.local.yml")
