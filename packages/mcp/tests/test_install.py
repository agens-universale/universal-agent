"""Тесты для install.py: диспатч по install.type и platform-фильтр."""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import patch

import pytest
from recommended_mcp import install as install_mod
from recommended_mcp.manifest import InstallSpec, MCPEntry
from recommended_mcp.runtime.venv import pip_install_target

_FAKE_MCP_PY = Path("/fake/mcp-env-python")


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
        with pip_install_target(_FAKE_MCP_PY):
            with patch("subprocess.run") as sub:
                install_mod.install_entry(entry, root=tmp_path)
    sub.assert_called_once()
    cmd = sub.call_args.args[0]
    assert cmd[0] == str(_FAKE_MCP_PY)
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


def test_pypi_calls_pip_install() -> None:
    entry = MCPEntry(
        name="duckduckgo_search",
        path=Path("./duckduckgo_search"),
        install=InstallSpec(type="pypi", package="ddgs-mcp==0.1.0"),
    )
    with pip_install_target(_FAKE_MCP_PY):
        with patch("recommended_mcp.install.query_installed_version", return_value=None):
            with patch("subprocess.run") as sub:
                install_mod.install_entry(entry, root=Path("."))
    cmd = sub.call_args.args[0]
    assert cmd == [
        str(_FAKE_MCP_PY),
        "-m",
        "pip",
        "install",
        "ddgs-mcp==0.1.0",
    ]


def test_pypi_skips_when_pinned_version_installed() -> None:
    entry = MCPEntry(
        name="duckduckgo_search",
        path=Path("./duckduckgo_search"),
        install=InstallSpec(type="pypi", package="ddgs-mcp==0.1.0"),
    )
    with patch("recommended_mcp.install.query_installed_version", return_value="0.1.0"):
        with patch("subprocess.run") as sub:
            install_mod.install_entry(entry, root=Path("."))
    assert not sub.called


def test_pypi_missing_package_raises() -> None:
    entry = MCPEntry(
        name="duckduckgo_search",
        path=Path("./duckduckgo_search"),
        install=InstallSpec(type="pypi", package=None),
    )
    with pytest.raises(install_mod.InstallError, match="install.package"):
        install_mod.install_entry(entry, root=Path("."))


def _source_entry(
    name: str = "x",
    source: str = "x-mcp",
    platforms: tuple[str, ...] = (),
) -> MCPEntry:
    return MCPEntry(
        name=name,
        path=Path(f"./{name}"),
        install=InstallSpec(
            type="source",
            platforms=platforms,
            source=source,  # имя каталога, не путь (см. test_mcp_git_install)
        ),
    )


def test_source_calls_pip_install(tmp_path: Path) -> None:
    # Раскладка колеса: вендоры ВНУТРИ каталога рецептов.
    recipes = tmp_path / "recommended_mcp"
    recipes.mkdir()
    src = recipes / "external" / "vendor" / "x-mcp"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    entry = _source_entry(source="x-mcp")
    with pip_install_target(_FAKE_MCP_PY):
        with patch("subprocess.run") as sub:
            install_mod.install_entry(entry, root=recipes)

    cmd = sub.call_args.args[0]
    assert cmd[0] == str(_FAKE_MCP_PY)
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert "-e" not in cmd
    assert str(src) in cmd


def test_source_editable_appends_dash_e(tmp_path: Path) -> None:
    recipes = tmp_path / "recommended_mcp"
    recipes.mkdir()
    src = recipes / "external" / "vendor" / "x-mcp"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    entry = _source_entry(source="x-mcp")
    with pip_install_target(_FAKE_MCP_PY):
        with patch("subprocess.run") as sub:
            install_mod.install_source_entry(entry, root=recipes, editable=True)

    cmd = sub.call_args.args[0]
    assert "-e" in cmd
    assert str(src) in cmd


def test_source_missing_pyproject_raises(tmp_path: Path) -> None:
    (tmp_path / "external" / "x").mkdir(parents=True)
    # pyproject.toml не создаём
    entry = _source_entry(source="x-mcp")
    with pytest.raises(install_mod.InstallError, match="pyproject.toml не найден"):
        install_mod.install_entry(entry, root=tmp_path)


def test_source_missing_source_raises(tmp_path: Path) -> None:
    # Эту валидацию ловит уже manifest.py, но защитимся и в install.py
    # на случай ручного построения MCPEntry в коде.
    entry = MCPEntry(
        name="x",
        path=Path("./x"),
        install=InstallSpec(type="source", source=None),
    )
    with pytest.raises(install_mod.InstallError, match="install.source"):
        install_mod.install_entry(entry, root=Path("."))


def test_npm_global_has_missing_npm(tmp_path, monkeypatch):
    monkeypatch.setattr(install_mod, "_node_install_paths", lambda dep: {"npm": tmp_path / "nope"})
    assert install_mod.npm_global_has("@playwright/mcp", tmp_path) is None


def test_npm_global_has_present(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    npm.write_text("", encoding="utf-8")
    monkeypatch.setattr(install_mod, "_node_install_paths", lambda dep: {"npm": npm})
    monkeypatch.setattr(
        install_mod.subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    assert install_mod.npm_global_has("@playwright/mcp", tmp_path) is True


def test_npm_global_has_absent(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    npm.write_text("", encoding="utf-8")
    monkeypatch.setattr(install_mod, "_node_install_paths", lambda dep: {"npm": npm})
    monkeypatch.setattr(
        install_mod.subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="")
    )
    assert install_mod.npm_global_has("@playwright/mcp", tmp_path) is False
