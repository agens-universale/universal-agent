"""Тесты для install.py: диспатч по install.type и platform-фильтр."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from recommended_mcp import install as install_mod
from recommended_mcp.manifest import InstallSpec, MCPEntry


def _entry(
    name: str = "x",
    install_type: str = "wheel",
    platforms: tuple[str, ...] = (),
) -> MCPEntry:
    return MCPEntry(
        name=name,
        path=Path(f"./{name}"),
        install=InstallSpec(type=install_type, platforms=platforms),
    )


def test_skips_when_platform_mismatch(capsys: pytest.CaptureFixture) -> None:
    entry = _entry(platforms=("linux",))
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("subprocess.run") as sub:
            install_mod.install_entry(entry, root=Path("."))
    assert not sub.called
    assert "skip" in capsys.readouterr().out.lower()


def test_runs_when_platform_matches(tmp_path: Path) -> None:
    pkg = tmp_path / "x"
    (pkg / "dist").mkdir(parents=True)
    wheel = pkg / "dist" / "x-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"fake")

    entry = _entry(platforms=("win32", "linux"))
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("subprocess.run") as sub:
            install_mod.install_entry(entry, root=tmp_path)
    sub.assert_called_once()
    cmd = sub.call_args.args[0]
    assert cmd[0] == sys.executable
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert str(wheel) in cmd


def test_wheel_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "x" / "dist").mkdir(parents=True)
    entry = _entry()
    with pytest.raises(install_mod.InstallError, match="no wheel found"):
        install_mod.install_entry(entry, root=tmp_path)


def test_wheel_multiple_raises(tmp_path: Path) -> None:
    pkg = tmp_path / "x" / "dist"
    pkg.mkdir(parents=True)
    (pkg / "x-0.1.0-py3-none-any.whl").write_bytes(b"a")
    (pkg / "x-0.2.0-py3-none-any.whl").write_bytes(b"b")
    entry = _entry()
    with pytest.raises(install_mod.InstallError, match="multiple wheels"):
        install_mod.install_entry(entry, root=tmp_path)


def test_docker_calls_compose_build(tmp_path: Path) -> None:
    pkg = tmp_path / "x"
    pkg.mkdir()
    (pkg / "docker-compose.yml").write_text("services: {}\n")
    entry = _entry(install_type="docker")
    with patch("subprocess.run") as sub:
        install_mod.install_entry(entry, root=tmp_path)
    cmd = sub.call_args.args[0]
    assert cmd[:3] == ["docker", "compose", "-f"]
    assert cmd[-1] == "build"


def test_docker_missing_compose_raises(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    entry = _entry(install_type="docker")
    with pytest.raises(install_mod.InstallError, match="docker-compose.yml not found"):
        install_mod.install_entry(entry, root=tmp_path)


def test_unknown_type_raises_fail_fast() -> None:
    entry = _entry(install_type="npm")
    with pytest.raises(install_mod.InstallError, match="not supported"):
        install_mod.install_entry(entry, root=Path("."))
