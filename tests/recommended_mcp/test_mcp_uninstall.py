from pathlib import Path

from recommended_mcp import install as inst
from recommended_mcp.manifest import MCPEntry, InstallSpec


def test_dist_name_pypi():
    e = MCPEntry(name="ddg", path=Path("."),
                 install=InstallSpec(type="pypi", package="ddgs-mcp==0.2.0"))
    assert inst._dist_name(e) == "ddgs-mcp"


def test_dist_name_source_reads_pyproject(tmp_path):
    src = tmp_path / "office-mcp"
    src.mkdir()
    (src / "pyproject.toml").write_text(
        "[project]\nname = \"office-mcp\"\nversion = \"1.0\"\n", encoding="utf-8")
    e = MCPEntry(name="office", path=Path("."),
                 install=InstallSpec(type="source", source=Path("office-mcp")))
    assert inst._dist_name(e, root=tmp_path) == "office-mcp"


def test_uninstall_entry_pypi_calls_pip(monkeypatch):
    calls = []
    monkeypatch.setattr(inst.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(inst, "pip_argv", lambda *a: ["PY", "-m", "pip", *a])
    e = MCPEntry(name="ddg", path=Path("."),
                 install=InstallSpec(type="pypi", package="ddgs-mcp==0.2.0"))
    inst.uninstall_entry(e, Path("."))
    assert calls and "uninstall" in calls[0] and "ddgs-mcp" in calls[0]


def test_uninstall_entry_npx_calls_npm(monkeypatch):
    calls = []
    monkeypatch.setattr(inst.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(inst, "_node_install_paths", lambda dep: {"npm": "NPM"})
    monkeypatch.setattr("config_modules.env_vars.gc_paths",
                        lambda: {"data": Path("."), "config": Path("."),
                                 "cache": Path("."), "state": Path(".")})
    e = MCPEntry(name="pw", path=Path("."),
                 install=InstallSpec(type="npx", npm_package="@playwright/mcp"))
    inst.uninstall_entry(e, Path("."))
    assert calls and "uninstall" in calls[0] and "@playwright/mcp" in calls[0]


def test_dist_name_wheel(tmp_path):
    dist = tmp_path / "rec" / "dist"
    dist.mkdir(parents=True)
    (dist / "office-mcp-1.0-py3-none-any.whl").write_text("", encoding="utf-8")
    e = MCPEntry(name="office", path=Path("rec"), install=InstallSpec(type="wheel"))
    assert inst._dist_name(e, root=tmp_path) == "office-mcp"


def test_dist_name_prefers_explicit_dist(tmp_path):
    # source-папки на диске НЕТ — _dist_name всё равно отдаёт install.dist
    e = MCPEntry(name="office", path=Path("."),
                 install=InstallSpec(type="source", source=Path("../external/office-mcp"),
                                     dist="office-mcp"))
    assert inst._dist_name(e, root=tmp_path) == "office-mcp"


def test_dist_name_fallback_pyproject_when_no_dist(tmp_path):
    src = tmp_path / "science-mcp"
    src.mkdir()
    (src / "pyproject.toml").write_text(
        '[project]\nname = "science-mcp"\nversion = "1"\n', encoding="utf-8")
    e = MCPEntry(name="science", path=Path("."),
                 install=InstallSpec(type="source", source=Path("science-mcp")))
    assert inst._dist_name(e, root=tmp_path) == "science-mcp"
