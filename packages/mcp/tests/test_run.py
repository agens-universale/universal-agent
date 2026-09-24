"""Тесты run.py: docker-compose up, skip для MCP без run-блока."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from recommended_mcp import run as run_mod
from recommended_mcp.manifest import InstallSpec, MCPEntry, RunSpec


def _entry_with_run(run: RunSpec | None = None) -> MCPEntry:
    return MCPEntry(
        name="x",
        path=Path("./x"),
        install=InstallSpec(type="docker"),
        run=run,
    )


def test_no_run_block_skips(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    entry = _entry_with_run(run=None)
    supervisor = run_mod.RunSupervisor(pids_dir=tmp_path)
    with patch("subprocess.run") as sub:
        run_mod.run_entry(entry, root=tmp_path, supervisor=supervisor)
    assert not sub.called
    assert "no run step" in capsys.readouterr().out


def test_docker_compose_up_detached(tmp_path: Path) -> None:
    pkg = tmp_path / "x"
    pkg.mkdir()
    (pkg / "docker-compose.yml").write_text("services: {}\n")
    entry = _entry_with_run(RunSpec(type="docker-compose", detach=True))
    supervisor = run_mod.RunSupervisor(pids_dir=tmp_path)
    with patch("subprocess.run") as sub:
        run_mod.run_entry(entry, root=tmp_path, supervisor=supervisor)
    cmd = sub.call_args.args[0]
    assert cmd[:3] == ["docker", "compose", "-f"]
    assert cmd[-2:] == ["up", "-d"]


def test_docker_compose_up_foreground(tmp_path: Path) -> None:
    pkg = tmp_path / "x"
    pkg.mkdir()
    (pkg / "docker-compose.yml").write_text("services: {}\n")
    entry = _entry_with_run(RunSpec(type="docker-compose", detach=False))
    supervisor = run_mod.RunSupervisor(pids_dir=tmp_path)
    with patch("subprocess.run") as sub:
        run_mod.run_entry(entry, root=tmp_path, supervisor=supervisor)
    cmd = sub.call_args.args[0]
    assert cmd[-1] == "up"
    assert "-d" not in cmd


def test_unknown_run_type_raises(tmp_path: Path) -> None:
    entry = _entry_with_run(RunSpec(type="systemd", detach=False))
    supervisor = run_mod.RunSupervisor(pids_dir=tmp_path)
    with pytest.raises(run_mod.RunError, match="not supported"):
        run_mod.run_entry(entry, root=Path("."), supervisor=supervisor)


def test_docker_compose_missing_file_raises(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    entry = _entry_with_run(RunSpec(type="docker-compose", detach=True))
    supervisor = run_mod.RunSupervisor(pids_dir=tmp_path)
    with pytest.raises(run_mod.RunError, match="docker-compose.yml not found"):
        run_mod.run_entry(entry, root=tmp_path, supervisor=supervisor)


def test_command_delegates_to_supervisor(tmp_path: Path) -> None:
    entry = _entry_with_run(
        RunSpec(
            type="command",
            command="science-mcp",
            args=("--transport", "http"),
        )
    )
    supervisor = run_mod.RunSupervisor(pids_dir=tmp_path)
    with patch.object(
        supervisor, "start_if_needed", return_value=True
    ) as start:
        started = run_mod.run_entry(
            entry, root=tmp_path, supervisor=supervisor
        )
    assert started is True
    start.assert_called_once()


def test_no_run_block_skips_with_supervisor(tmp_path: Path) -> None:
    entry = _entry_with_run(run=None)
    supervisor = run_mod.RunSupervisor(pids_dir=tmp_path)
    with patch.object(supervisor, "start_if_needed") as start:
        started = run_mod.run_entry(
            entry, root=tmp_path, supervisor=supervisor
        )
    assert started is False
    start.assert_not_called()
